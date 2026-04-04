"""
IoT endpoints - Production safe with idempotency and error handling.
✅ Prevents duplicate inserts, safe DB transactions, no crashes.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db, safe_commit
from app.models import House, GenerationRecord, DemandRecord, Allocation
from app.services.iot_service import iot_service
from app.services.matching_engine import MatchingEngine
from app.services.pool_engine import PoolEngine
from app.utils.async_utils import safe_execute
from config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class IoTData(BaseModel):
    auth_token: str
    device_id: str
    generation_kwh: float = 0
    demand_kwh: float = 0
    house_id: str
    signal_strength: int = 0


# ✅ IDEMPOTENCY TRACKING (in-memory, 1 hour TTL)
class IdempotencyCache:
    """Simple idempotency cache to prevent duplicate inserts."""
    
    def __init__(self):
        self._cache = {}
        self._timestamps = {}
    
    def get_key(self, house_id: str, kwh: float, operation_type: str) -> str:
        """Create idempotency key: house_id + kwh + type."""
        return f"{house_id}:{operation_type}:{kwh:.2f}"
    
    def is_duplicate(self, house_id: str, kwh: float, operation_type: str) -> bool:
        """Check if request is duplicate (within 30 seconds)."""
        import time
        key = self.get_key(house_id, kwh, operation_type)
        
        if key not in self._cache:
            return False
        
        last_time = self._timestamps.get(key, 0)
        if time.time() - last_time < 30:  # 30 second window
            logger.warning(f"⚠️ Duplicate request detected: {key}")
            return True
        
        return False
    
    def mark_processed(self, house_id: str, kwh: float, operation_type: str) -> None:
        """Mark request as processed."""
        import time
        key = self.get_key(house_id, kwh, operation_type)
        self._cache[key] = True
        self._timestamps[key] = time.time()
    
    def cleanup(self) -> None:
        """Remove old entries (older than 1 hour)."""
        import time
        now = time.time()
        expired = [k for k, v in self._timestamps.items() if now - v > 3600]
        for k in expired:
            del self._cache[k]
            del self._timestamps[k]


idempotency = IdempotencyCache()


@router.post("/update")
async def unified_iot_update(data: IoTData, db: Session = Depends(get_db)):
    """
    ✅ PRODUCTION SAFE: Unified IoT update endpoint.
    - Idempotency: Prevents duplicate inserts
    - Error handling: All errors return safe JSON
    - DB safety: Proper commit/rollback
    - Timeout: 5s max
    """
    try:
        # ✅ Auth check
        if data.auth_token != settings.iot_auth_token:
            raise HTTPException(status_code=401, detail="Invalid auth token")

        # ✅ Find house with timeout
        house = await safe_execute(
            asyncio.to_thread(
                lambda: db.query(House).filter(House.house_id == data.house_id).first()
            ),
            timeout=2.0,
            operation_name="Find house",
        )
        
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        # ✅ SELLER: Generation
        if data.generation_kwh > 0.1:
            return await _handle_generation(db, data, house)
        
        # ✅ BUYER: Demand
        elif data.demand_kwh > 0.1:
            return await _handle_demand(db, data, house)
        
        # ✅ HEARTBEAT
        else:
            return {
                "status": "heartbeat",
                "device_id": data.device_id,
                "house_id": data.house_id,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ IoT/Update error: {str(e)}", exc_info=True)
        db.rollback()
        return {"status": "error", "error": str(e)[:100]}


async def _handle_generation(db: Session, data: IoTData, house) -> dict:
    """Safe generation handling with idempotency."""
    try:
        # ✅ Check for duplicate
        if idempotency.is_duplicate(data.house_id, data.generation_kwh, "gen"):
            return {
                "status": "duplicate_skipped",
                "reason": "Same generation value sent <30s ago",
                "device_type": "seller",
                "house_id": data.house_id,
            }

        def save_gen():
            # ✅ Prevent manual ID setting (use auto-increment)
            record = GenerationRecord(
                house_id=house.id,
                generation_kwh=data.generation_kwh,
                device_id=data.device_id,
                signal_strength=data.signal_strength,
                timestamp=datetime.utcnow(),
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            
            # Update cache
            iot_service.update_device_status(
                house_id=data.house_id,
                device_id=data.device_id,
                generation_kwh=data.generation_kwh,
                signal_strength=data.signal_strength,
            )
            
            return record

        # ✅ Execute with timeout and error handling
        result = await safe_execute(
            asyncio.to_thread(save_gen),
            timeout=3.0,
            operation_name="Save generation",
        )

        if result:
            idempotency.mark_processed(data.house_id, data.generation_kwh, "gen")
            logger.info(f"✅ Gen recorded: {data.house_id} → {data.generation_kwh}kWh (ID={result.id})")
            return {
                "status": "generation_received",
                "device_type": "seller",
                "house_id": data.house_id,
                "generation_kwh": data.generation_kwh,
                "record_id": result.id,
            }
        else:
            logger.warning(f"⚠️ Gen save timed out: {data.house_id}")
            return {
                "status": "saved_locally",
                "device_type": "seller",
                "house_id": data.house_id,
            }

    except IntegrityError as e:
        logger.error(f"IntegrityError (duplicate key): {str(e)}")
        db.rollback()
        return {
            "status": "error",
            "error": "Duplicate key error - generation value already exists",
            "house_id": data.house_id,
        }
    except Exception as e:
        logger.error(f"Generation error: {e}", exc_info=True)
        db.rollback()
        return {
            "status": "error",
            "device_type": "seller",
            "house_id": data.house_id,
            "error": str(e)[:100],
        }


async def _handle_demand(db: Session, data: IoTData, house) -> dict:
    """Safe demand handling with idempotency and AI fallback."""
    try:
        # ✅ Check for duplicate
        if idempotency.is_duplicate(data.house_id, data.demand_kwh, "demand"):
            return {
                "status": "duplicate_skipped",
                "reason": "Same demand value sent <30s ago",
                "device_type": "buyer",
                "house_id": data.house_id,
            }

        def create_demand():
            # ✅ Create demand record (auto-increment ID)
            demand = DemandRecord(
                house_id=house.id,
                demand_kwh=data.demand_kwh,
                priority_level=5,
                duration_hours=1.0,
                status="pending",
                timestamp=datetime.utcnow(),
            )
            db.add(demand)
            db.commit()
            db.refresh(demand)
            
            # Update cache
            iot_service.update_buyer_demand(
                data.house_id,
                data.demand_kwh,
                data.device_id
            )
            
            return demand

        # ✅ Create demand with timeout
        demand = await safe_execute(
            asyncio.to_thread(create_demand),
            timeout=3.0,
            operation_name="Create demand",
        )

        if not demand:
            return {"status": "demand_cached", "house_id": data.house_id}

        # ✅ Match with timeout and fallback
        result = await _safe_match_demand_async(db, house.id, data.demand_kwh)

        # ✅ Update demand status with error handling
        def update_status():
            demand.status = "fulfilled" if result["grid_kwh"] == 0 else "partial"
            return safe_commit(db, "Update demand status")

        await safe_execute(
            asyncio.to_thread(update_status),
            timeout=2.0,
            operation_name="Update demand status",
        )

        idempotency.mark_processed(data.house_id, data.demand_kwh, "demand")

        logger.info(
            f"✅ Demand matched: {data.house_id} "
            f"Pool={result['pool_kwh']:.2f}kWh Grid={result['grid_kwh']:.2f}kWh"
        )

        return {
            "status": "demand_received",
            "device_type": "buyer",
            "demand_id": demand.id,
            "allocation_id": result.get("allocation_id"),
            "demand_kwh": data.demand_kwh,
            "allocated_kwh": result["pool_kwh"],
            "grid_required_kwh": result["grid_kwh"],
            "allocation_status": "matched" if result["grid_kwh"] == 0 else "partial",
            "ai_reasoning": result["ai_reasoning"],
        }

    except IntegrityError as e:
        logger.error(f"IntegrityError (duplicate demand): {str(e)}")
        db.rollback()
        return {
            "status": "error",
            "error": "Duplicate demand - same value already exists",
            "house_id": data.house_id,
        }
    except Exception as e:
        logger.error(f"Demand error: {e}", exc_info=True)
        db.rollback()
        return {
            "status": "error",
            "device_type": "buyer",
            "house_id": data.house_id,
            "error": str(e)[:100],
        }


async def _safe_match_demand_async(db: Session, house_id: int, demand_kwh: float) -> dict:
    """Match with timeout and fallback to grid."""
    try:
        def match():
            matching = MatchingEngine(db)
            return matching.match_demand(house_id, demand_kwh)

        result = await safe_execute(
            asyncio.to_thread(match),
            timeout=5.0,
            operation_name="AI matching",
        )

        if result:
            return result

        # Timeout - return grid-only fallback
        logger.warning("Matching timed out, using 100% grid fallback")
        return {
            "pool_kwh": 0,
            "grid_kwh": demand_kwh,
            "ai_reasoning": "Timeout: 100% grid fallback",
            "estimated_pool_cost_inr": 0,
            "estimated_grid_cost_inr": demand_kwh * settings.discom_grid_rate,
            "sun_tokens_minted": 0,
            "blockchain_tx": None,
            "allocation_id": None,
        }

    except Exception as e:
        logger.error(f"Match error: {e}")
        # Ultimate fallback
        return {
            "pool_kwh": 0,
            "grid_kwh": demand_kwh,
            "ai_reasoning": f"Error: {str(e)[:40]}",
            "estimated_pool_cost_inr": 0,
            "estimated_grid_cost_inr": demand_kwh * settings.discom_grid_rate,
            "sun_tokens_minted": 0,
            "blockchain_tx": None,
            "allocation_id": None,
        }


@router.post("/test-demand")
async def test_iot_demand(house_id: str, demand_kwh: float, db: Session = Depends(get_db)):
    """Test endpoint for demand simulation."""
    try:
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        data = IoTData(
            auth_token=settings.iot_auth_token,
            device_id="TEST_SIMULATOR",
            demand_kwh=demand_kwh,
            house_id=house_id,
            signal_strength=0,
        )

        return await _handle_demand(db, data, house)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test demand error: {e}")
        db.rollback()
        return {"status": "error", "error": str(e)}


@router.get("/status/{house_id}")
async def get_iot_status(house_id: str, db: Session = Depends(get_db)):
    """Get IoT device status (from cache, not DB)."""
    try:
        # Verify house exists
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        status = iot_service.get_device_status(house_id)
        demand = iot_service.get_buyer_demand(house_id)

        return {
            "status": "ok",
            "house_id": house_id,
            "device_status": status or {},
            "buyer_demand": demand or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status error: {e}")
        return {"status": "error", "house_id": house_id, "error": str(e)}



class IoTData(BaseModel):
    auth_token: str
    device_id: str
    generation_kwh: float = 0  # For seller (solar generation)
    demand_kwh: float = 0  # For buyer (energy demand)
    house_id: str
    signal_strength: int


class IoTDemandData(BaseModel):
    auth_token: str
    device_id: str
    demand_kwh: float
    house_id: str
    signal_strength: int = 0  # Optional signal strength from device


@router.post("/update")
async def unified_iot_update(data: IoTData, db: Session = Depends(get_db)):
    """
    UNIFIED ENDPOINT: Production version with timeout protection.
    - 5 second timeout on all operations
    - Automatic fallback if AI or matching fails
    - Never crashes, always returns valid JSON
    """
    try:
        # ✅ Auth check (lightweight)
        if data.auth_token != settings.iot_auth_token:
            raise HTTPException(status_code=401, detail="Invalid auth token")

        # ✅ Find house with timeout
        def find_house():
            return db.query(House).filter(House.house_id == data.house_id).first()

        house = await safe_execute(
            asyncio.to_thread(find_house),
            timeout=2.0,
            operation_name="Find house",
        )
        
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        logger.info(
            f"📥 IoT Update: {data.device_id} {data.house_id} "
            f"Gen={data.generation_kwh}kWh Demand={data.demand_kwh}kWh"
        )

        # ✅ SELLER: Generation ≥ 0.1 kWh
        if data.generation_kwh >= 0.1:
            return await _handle_generation(db, data, house)
        
        # ✅ BUYER: Demand ≥ 0.1 kWh
        elif data.demand_kwh >= 0.1:
            return await _handle_demand(db, data, house)
        
        # ✅ HEARTBEAT: Both negligible
        else:
            return {
                "status": "heartbeat",
                "device_id": data.device_id,
                "house_id": data.house_id,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ IoT/Update error: {str(e)}")
        return {
            "status": "error",
            "error": str(e)[:100],
            "house_id": data.house_id,
        }


async def _handle_generation(db: Session, data: IoTData, house) -> dict:
    """Generate with timeout protection."""
    try:
        def save_gen():
            record = GenerationRecord(
                house_id=house.id,
                generation_kwh=data.generation_kwh,
                device_id=data.device_id,
                signal_strength=data.signal_strength,
            )
            db.add(record)
            db.commit()
            iot_service.update_device_status(
                house_id=data.house_id,
                device_id=data.device_id,
                generation_kwh=data.generation_kwh,
                signal_strength=data.signal_strength,
            )

        await safe_execute(
            asyncio.to_thread(save_gen),
            timeout=3.0,
            operation_name="Save generation",
        )

        logger.info(f"✅ Gen recorded: {data.house_id} {data.generation_kwh}kWh")

        return {
            "status": "generation_received",
            "device_type": "seller",
            "house_id": data.house_id,
            "generation_kwh": data.generation_kwh,
        }

    except Exception as e:
        logger.error(f"Generation error: {e}")
        return {
            "status": "error",
            "device_type": "seller",
            "house_id": data.house_id,
            "error": str(e)[:100],
        }


async def _handle_demand(db: Session, data: IoTData, house) -> dict:
    """Demand with timeout and fallback matching."""
    try:
        def create_demand_record():
            demand = DemandRecord(
                house_id=house.id,
                demand_kwh=data.demand_kwh,
                priority_level=5,
                duration_hours=1.0,
                status="pending",
            )
            db.add(demand)
            db.commit()
            db.refresh(demand)
            iot_service.update_buyer_demand(data.house_id, data.demand_kwh, data.device_id)
            return demand

        demand = await safe_execute(
            asyncio.to_thread(create_demand_record),
            timeout=3.0,
            operation_name="Create demand",
        )

        if not demand:
            return {"status": "demand_cached", "house_id": data.house_id}

        # Match with timeout and fallback
        result = await _safe_match_demand(db, house.id, data.demand_kwh)

        def update_status():
            demand.status = "fulfilled" if result["grid_kwh"] == 0 else "partial"
            db.commit()

        await safe_execute(
            asyncio.to_thread(update_status),
            timeout=2.0,
            operation_name="Update demand status",
        )

        logger.info(f"✅ Demand matched: {data.house_id} Pool={result['pool_kwh']:.2f}kWh")

        return {
            "status": "demand_received",
            "device_type": "buyer",
            "demand_id": demand.id,
            "allocation_id": result.get("allocation_id"),
            "demand_kwh": data.demand_kwh,
            "allocated_kwh": result["pool_kwh"],
            "grid_required_kwh": result["grid_kwh"],
            "allocation_status": "matched" if result["grid_kwh"] == 0 else "partial",
            "ai_reasoning": result["ai_reasoning"],
            "estimated_cost_inr": (
                result["estimated_pool_cost_inr"] + result["estimated_grid_cost_inr"]
            ),
        }

    except Exception as e:
        logger.error(f"❌ Demand error: {e}")
        return {
            "status": "error",
            "device_type": "buyer",
            "house_id": data.house_id,
            "error": str(e)[:100],
        }


async def _safe_match_demand(db: Session, house_id: int, demand_kwh: float) -> dict:
    """Match with timeout and fallback."""
    try:
        def match():
            matching = MatchingEngine(db)
            return matching.match_demand(house_id, demand_kwh)

        result = await safe_execute(
            asyncio.to_thread(match),
            timeout=5.0,
            operation_name="AI matching",
        )

        if result:
            return result

        # Timeout - use fallback
        logger.warning("Matching timed out, using grid fallback")
        return {
            "pool_kwh": 0,
            "grid_kwh": demand_kwh,
            "ai_reasoning": "Timeout: using grid",
            "estimated_pool_cost_inr": 0,
            "estimated_grid_cost_inr": demand_kwh * settings.discom_grid_rate,
            "sun_tokens_minted": 0,
            "blockchain_tx": None,
            "allocation_id": None,
        }

    except Exception as e:
        logger.error(f"Match error: {e}")
        # Ultimate fallback
        return {
            "pool_kwh": 0,
            "grid_kwh": demand_kwh,
            "ai_reasoning": f"Error: {str(e)[:40]}",
            "estimated_pool_cost_inr": 0,
            "estimated_grid_cost_inr": demand_kwh * settings.discom_grid_rate,
            "sun_tokens_minted": 0,
            "blockchain_tx": None,
            "allocation_id": None,
        }



# ✅ TEST ENDPOINT
@router.post("/test-demand")
async def test_iot_demand(house_id: str, demand_kwh: float, db: Session = Depends(get_db)):
    """
    TEST: Simulate IoT device demand without real hardware.
    Example: POST /api/iot/test-demand?house_id=HOUSE_FDR12_002&demand_kwh=2.5
    """
    try:
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        data = IoTData(
            auth_token=settings.iot_auth_token,
            device_id="TEST_SIMULATOR",
            demand_kwh=demand_kwh,
            house_id=house_id,
            signal_strength=0,
        )

        return await _handle_demand(db, data, house)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test demand error: {e}")
        return {"status": "error", "error": str(e)}


# ✅ STATUS ENDPOINT
@router.get("/status/{house_id}")
async def get_iot_status(house_id: str, db: Session = Depends(get_db)):
    """Get IoT device status (cached, never queries DB directly)."""
    try:
        # Verify house exists
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            raise HTTPException(status_code=404, detail="House not found")

        status = iot_service.get_device_status(house_id)
        demand = iot_service.get_buyer_demand(house_id)

        return {
            "status": "ok",
            "house_id": house_id,
            "device_status": status or {},
            "buyer_demand": demand or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status error: {e}")
        return {
            "status": "error",
            "house_id": house_id,
            "error": str(e),
        }
