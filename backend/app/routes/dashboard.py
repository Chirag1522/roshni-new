"""
Dashboard endpoints with in-memory caching (TTL: 3 seconds).
"""
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from app.database import get_db
from app.schemas import DashboardResponse, HouseGenerationSummary, HouseDemandSummary, LivePoolState
from app.models import House, GenerationRecord, DemandRecord, PoolState
from app.services.pool_engine import PoolEngine
from app.utils.async_utils import request_cache, safe_execute
from config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/{house_id}", response_model=DashboardResponse)
async def get_dashboard(house_id: str, db: Session = Depends(get_db)):
    """
    Get dashboard data with 3-second caching.
    Reduces database load during polling.
    """
    # ✅ Check cache first
    cache_key = f"dashboard_{house_id}"
    cached = request_cache.get(cache_key)
    if cached:
        logger.debug(f"📦 Dashboard cache hit: {house_id}")
        return cached

    try:
        async def fetch_dashboard():
            return await _fetch_dashboard_data(db, house_id)

        result = await safe_execute(
            fetch_dashboard(),
            timeout=10.0,  # ✅ Increased from 5s to 10s (database queries can be slow)
            operation_name="Fetch dashboard",
        )

        if result:
            # ✅ Cache for 3 seconds
            request_cache.set(cache_key, result, ttl_seconds=3)
            logger.debug(f"✅ Dashboard cached: {house_id}")
            return result

        # Timeout - return minimal response with cached fallback
        logger.warning(f"Dashboard timeout for {house_id}")
        # Return last cached result if available to avoid 504
        if cached:
            return cached
        raise HTTPException(status_code=504, detail="Dashboard fetch timeout")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _fetch_dashboard_data(db: Session, house_id: str) -> DashboardResponse:
    """Fetch dashboard data (blocking operations run in thread)."""
    
    def fetch_sync():
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        # Generation summary (today) - LIMIT to last 1000 records for performance
        today = datetime.utcnow().date()
        today_generation = db.query(GenerationRecord).filter(
            GenerationRecord.house_id == house.id,
            GenerationRecord.created_at >= datetime.combine(today, datetime.min.time()),
        ).order_by(GenerationRecord.created_at.desc()).limit(1000).all()

        today_gen_kwh = sum(g.generation_kwh for g in today_generation)

        generation_summary = HouseGenerationSummary(
            house_id=house_id,
            today_generated_kwh=today_gen_kwh,
            this_month_generated_kwh=house.current_month_generation_kwh or 0,
            average_hourly_kw=today_gen_kwh / 24 if today_gen_kwh > 0 else 0,
            peak_generation_kw=max(
                (g.generation_kwh for g in today_generation), default=0
            ),
            latest_generation_timestamp=max(
                (g.created_at for g in today_generation), default=None
            ),
        ) if today_generation else None

        # Demand summary (today) - LIMIT to last 1000 records for performance
        today_demand = db.query(DemandRecord).filter(
            DemandRecord.house_id == house.id,
            DemandRecord.created_at >= datetime.combine(today, datetime.min.time()),
        ).order_by(DemandRecord.created_at.desc()).limit(1000).all()

        today_demand_kwh = sum(d.demand_kwh for d in today_demand)

        demand_summary = HouseDemandSummary(
            house_id=house_id,
            today_demand_kwh=today_demand_kwh,
            this_month_demand_kwh=today_demand_kwh * 30,
            average_hourly_kw=today_demand_kwh / 24 if today_demand_kwh > 0 else 0,
            allocation_rate=0.8,
            grid_dependency_rate=0.2,
        ) if today_demand else None

        # Live pool state (READ ONLY)
        pool_engine = PoolEngine(db)
        pool_state = pool_engine.get_pool_state(house.feeder_id)

        if pool_state:
            live_pool = LivePoolState(
                feeder_code=house.feeder.feeder_code,
                current_supply_kwh=pool_state.get("current_supply_kwh", 0),
                current_demand_kwh=pool_state.get("current_demand_kwh", 0),
                grid_drawdown_kwh=pool_state.get("grid_drawdown", 0),
                today_fulfilled_kwh=pool_state.get("today_fulfilled_kwh", 0),
                today_trade_count=pool_state.get("today_trade_count", 0),
                timestamp=pool_state.get("timestamp", datetime.utcnow()),
            )
        else:
            live_pool = LivePoolState(
                feeder_code=house.feeder.feeder_code,
                current_supply_kwh=0,
                current_demand_kwh=0,
                grid_drawdown_kwh=0,
                today_fulfilled_kwh=0,
                today_trade_count=0,
                timestamp=datetime.utcnow(),
            )

        # Earnings/savings estimate
        allocation_earnings = (
            today_gen_kwh * settings.solar_pool_rate
            if house.prosumer_type in ["seller", "generator", "prosumer"]
            else 0
        )
        allocation_savings = (
            today_demand_kwh * max(0, settings.discom_grid_rate - settings.solar_pool_rate)
            if house.prosumer_type in ["buyer", "consumer", "prosumer"]
            else 0
        )

        return DashboardResponse(
            house_id=house_id,
            feeder_code=house.feeder.feeder_code,
            prosumer_type=house.prosumer_type,
            generation_summary=generation_summary,
            demand_summary=demand_summary,
            live_pool_state=live_pool,
            allocation_earnings_estimate_inr=allocation_earnings,
            allocation_savings_estimate_inr=allocation_savings,
        )

    return await safe_execute(
        asyncio.to_thread(fetch_sync),
        timeout=5.0,
        operation_name="Fetch dashboard sync",
    )



@router.get("/pool/{feeder_code}")
async def get_pool_state(feeder_code: str, db: Session = Depends(get_db)):
    """Get current feeder pool state."""
    from app.models import Feeder

    feeder = db.query(Feeder).filter(Feeder.feeder_code == feeder_code).first()
    if not feeder:
        raise HTTPException(status_code=404, detail="Feeder not found")

    pool_engine = PoolEngine(db)
    state_data = pool_engine.get_pool_state(feeder.id)

    return {
        "feeder_code": feeder_code,
        **(state_data or {}),
        "timestamp": datetime.utcnow(),
    }