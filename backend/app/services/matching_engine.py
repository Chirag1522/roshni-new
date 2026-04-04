"""
AI-powered matching engine for supply-demand allocation.
✅ Fixed to use async AI service with proper error handling.
"""
import asyncio
import logging
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from app.models import House, GenerationRecord, DemandRecord, Allocation, PoolState
from app.services.pool_engine import PoolEngine
from app.services.ai_service import ai_service

logger = logging.getLogger(__name__)


class MatchingEngine:
    """Matches solar supply to consumer demand with AI optimization."""

    def __init__(self, db: Session):
        self.db = db
        self.pool_engine = PoolEngine(db)

    def match_demand(self, house_id: int, demand_kwh: float) -> dict:
        """
        Match consumer demand with pool supply.
        SYNC version that wraps async AI service.
        """
        # Run async matching in thread
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        result = loop.run_until_complete(self._match_demand_async(house_id, demand_kwh))
        return result

    async def _match_demand_async(self, house_id: int, demand_kwh: float) -> dict:
        """Async version of match_demand with proper error handling."""
        try:
            house = self.db.query(House).filter(House.id == house_id).first()
            if not house:
                raise ValueError(f"House {house_id} not found")

            # Get live pool state
            pool_state = self.pool_engine.get_pool_state(house.feeder_id)
            available_supply = pool_state.get("current_supply_kwh", 0)

            # Get AI allocation (with timeout and fallback built in)
            ai_recommendation = await ai_service.get_allocation_strategy(
                available_pool_kwh=available_supply,
                demand_kwh=demand_kwh,
                grid_rate_inr=self._get_grid_rate(),
                pool_rate_inr=self._get_pool_rate(),
                house_priority=5,
            )

            pool_allocation_kwh = min(
                ai_recommendation.get("pool_kwh", 0),
                available_supply,
                demand_kwh,
            )
            grid_fallback_kwh = max(0, demand_kwh - pool_allocation_kwh)

            # Save allocation record
            allocation = Allocation(
                house_id=house_id,
                allocated_kwh=pool_allocation_kwh,
                source_type="pool" if pool_allocation_kwh > 0 else "grid",
                status="completed",
                ai_reasoning=ai_recommendation.get("reasoning", ""),
            )
            self.db.add(allocation)

            # Update house monthly SUN received
            if pool_allocation_kwh > 0:
                house.current_month_sun_received = (
                    (house.current_month_sun_received or 0) + pool_allocation_kwh
                )

            self.db.commit()
            self.db.refresh(allocation)

            # Mint SUN tokens to buyer if pool allocation happened
            blockchain_result = {"status": "skipped"}
            if pool_allocation_kwh > 0 and house.algorand_address and house.opt_in_sun_asa:
                try:
                    from app.services.blockchain_service import BlockchainService
                    blockchain = BlockchainService()
                    blockchain_result = blockchain.transfer_sun_asa(
                        recipient_address=house.algorand_address,
                        amount_kwh=pool_allocation_kwh,
                        reason=f"pool_allocation_{allocation.id}",
                    )
                    if blockchain_result.get("status") == "submitted":
                        allocation.transaction_hash = blockchain_result.get("tx_id")
                        self.db.commit()
                        logger.info(
                            f"SUN minted: {pool_allocation_kwh:.2f} SUN → "
                            f"{house.house_id} (TX: {blockchain_result.get('tx_id')})"
                        )
                except Exception as e:
                    logger.warning(f"SUN mint skipped: {e}")

            # Credit sellers
            self._credit_sellers(house.feeder_id, pool_allocation_kwh)

            # Update pool state
            self.pool_engine.update_pool_state(house.feeder_id)

            logger.info(
                f"Demand matched: {house.house_id} → "
                f"Pool={pool_allocation_kwh:.2f}kWh, Grid={grid_fallback_kwh:.2f}kWh"
            )

            return {
                "allocation_id": allocation.id,
                "pool_kwh": pool_allocation_kwh,
                "grid_kwh": grid_fallback_kwh,
                "ai_reasoning": ai_recommendation.get("reasoning", ""),
                "estimated_pool_cost_inr": round(pool_allocation_kwh * self._get_pool_rate(), 2),
                "estimated_grid_cost_inr": round(grid_fallback_kwh * self._get_grid_rate(), 2),
                "sun_tokens_minted": pool_allocation_kwh if pool_allocation_kwh > 0 else 0,
                "blockchain_tx": blockchain_result.get("tx_id"),
            }

        except Exception as e:
            logger.error(f"Match error: {e}")
            # Fallback: allocate all from grid
            return {
                "allocation_id": None,
                "pool_kwh": 0,
                "grid_kwh": demand_kwh,
                "ai_reasoning": f"Error fallback: {str(e)[:50]}",
                "estimated_pool_cost_inr": 0,
                "estimated_grid_cost_inr": demand_kwh * self._get_grid_rate(),
                "sun_tokens_minted": 0,
                "blockchain_tx": None,
            }

    def _credit_sellers(self, feeder_id: int, pool_kwh_sold: float):
        """Proportionally credit SUN tokens to active generators."""
        if pool_kwh_sold <= 0:
            return

        seller_window_start = datetime.utcnow() - timedelta(minutes=15)

        # Find active generating houses
        active_sellers = []
        seller_houses = self.db.query(House).filter(
            House.feeder_id == feeder_id,
            House.prosumer_type.in_(["seller", "generator", "prosumer"]),
            House.is_active == True,
        ).all()

        for h in seller_houses:
            latest = self.db.query(GenerationRecord).filter(
                GenerationRecord.house_id == h.id,
                GenerationRecord.created_at >= seller_window_start,
            ).order_by(GenerationRecord.created_at.desc()).first()
            if latest:
                active_sellers.append((h, latest.generation_kwh))

        if not active_sellers:
            return

        total_gen = sum(g for _, g in active_sellers)
        if total_gen == 0:
            return

        for seller_house, gen_kwh in active_sellers:
            share = gen_kwh / total_gen
            seller_house.current_month_sun_minted = (
                (seller_house.current_month_sun_minted or 0) + pool_kwh_sold * share
            )

        self.db.commit()

    def _get_grid_rate(self) -> float:
        """Get current grid rate from settings."""
        from config import settings
        return settings.discom_grid_rate

    def _get_pool_rate(self) -> float:
        """Get current pool rate from settings."""
        from config import settings
        return settings.solar_pool_rate