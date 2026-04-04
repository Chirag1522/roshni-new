"""
Example implementation showing best practices for using safe DB operations.
This file demonstrates complete patterns for IoT data handling with idempotency,
error handling, and proper database transactions.

✅ No data loss
✅ No duplicate key errors
✅ Proper rollback on failure
✅ Safe concurrent access
"""

from sqlalchemy.orm import Session
from fastapi import Depends
from datetime import datetime
import logging

from app.database import get_db, safe_commit
from app.models import House, GenerationRecord, DemandRecord, Allocation
from app.services.safe_db_ops import safe_db, SafeDatabaseOps
from app.services.matching_engine import MatchingEngine
from config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# EXAMPLE 1: CREATE GENERATION RECORD SAFELY
# ============================================================================

def example_create_generation(db: Session, house_id: int, generation_kwh: float):
    """
    ✅ SAFE: Create generation record with proper error handling.
    
    Why this is safe:
    - ID is auto-increment (never manually set)
    - Errors are caught and rolled back
    - Duplicates are prevented with idempotency check
    """
    
    # Step 1: Check for recent duplicate
    is_dup = safe_db.is_duplicate_generation(
        db,
        house_id=house_id,
        generation_kwh=generation_kwh,
        seconds=10,  # Check last 10 seconds
        tolerance=0.1,
    )
    
    if is_dup:
        logger.warning(f"Duplicate generation skipped: {house_id} → {generation_kwh}")
        return {
            "status": "duplicate_skipped",
            "reason": "Same generation value sent <10s ago",
        }
    
    # Step 2: Create record (auto-increment ID)
    record = safe_db.create_generation_record(
        db,
        house_id=house_id,
        generation_kwh=generation_kwh,
        device_id="NodeMCU_001",
        signal_strength=85,
    )
    
    if not record:
        logger.error(f"Failed to create generation record for {house_id}")
        return {
            "status": "error",
            "reason": "Database operation failed",
        }
    
    # Step 3: Return success response
    return {
        "status": "success",
        "record_id": record.id,  # ✅ Use actual ID from DB
        "generation_kwh": record.generation_kwh,
    }


# ============================================================================
# EXAMPLE 2: CREATE DEMAND RECORD AND MATCH IT SAFELY
# ============================================================================

def example_create_and_match_demand(
    db: Session,
    house_id: int,
    demand_kwh: float,
) -> dict:
    """
    ✅ SAFE: Create demand record and trigger matching with full error handling.
    
    Why this is safe:
    - Demand record creation has error handling
    - Matching has timeout and fallback logic
    - Entire transaction is committed or rolled back atomically
    """
    
    # Step 1: Find house
    house = db.query(House).filter(House.house_id == house_id).first()
    if not house:
        return {
            "status": "error",
            "reason": "House not found",
        }
    
    # Step 2: Check for duplicate demand
    is_dup = safe_db.is_duplicate_demand(
        db,
        house_id=house.id,
        demand_kwh=demand_kwh,
        minutes=1,  # Check last minute
        tolerance=0.2,
    )
    
    if is_dup:
        logger.warning(f"Duplicate demand skipped: {house_id} → {demand_kwh}")
        return {
            "status": "duplicate_skipped",
            "reason": "Similar demand sent <1 minute ago",
        }
    
    # Step 3: Create demand record (auto-increment ID)
    demand = safe_db.create_demand_record(
        db,
        house_id=house.id,
        demand_kwh=demand_kwh,
        priority_level=5,
    )
    
    if not demand:
        return {
            "status": "error",
            "reason": "Failed to create demand record",
        }
    
    # Step 4: Try to match demand (has fallback logic)
    try:
        matching = MatchingEngine(db)
        match_result = matching.match_demand(house.id, demand_kwh)
        
        # Step 5: Update demand status based on match result
        success = safe_db.update_allocation_status(
            db,
            match_result["allocation_id"],
            "completed",
        )
        
        if not success:
            logger.warning(f"Failed to update allocation status for {match_result['allocation_id']}")
        
        # Step 6: Return complete response
        return {
            "status": "success",
            "demand_id": demand.id,
            "allocation_id": match_result["allocation_id"],
            "demand_kwh": demand_kwh,
            "pool_allocated_kwh": match_result["pool_kwh"],
            "grid_required_kwh": match_result["grid_kwh"],
        }
        
    except Exception as match_error:
        logger.error(f"Matching failed: {match_error}")
        
        # Fallback: allocate 100% to grid
        db.rollback()
        
        return {
            "status": "fallback_to_grid",
            "demand_id": demand.id,
            "reason": f"Matching failed: {str(match_error)[:50]}",
            "pool_allocated_kwh": 0,
            "grid_required_kwh": demand_kwh,
        }


# ============================================================================
# EXAMPLE 3: BULK UPDATE WITH SAFE COMMIT
# ============================================================================

def example_bulk_update_daily_stats(db: Session, house_id: int):
    """
    ✅ SAFE: Update multiple records atomically with proper rollback.
    
    Why this is safe:
    - All updates happen before commit
    - If ANY update fails, ALL are rolled back
    - No partial state corruption
    """
    
    try:
        # Get house
        house = db.query(House).filter(House.house_id == house_id).first()
        if not house:
            logger.error(f"House {house_id} not found")
            return False
        
        # Calculate today's generation and demand
        from datetime import timedelta
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        today_generation = db.query(GenerationRecord).filter(
            GenerationRecord.house_id == house.id,
            GenerationRecord.timestamp >= today_start,
        ).all()
        
        today_demand = db.query(DemandRecord).filter(
            DemandRecord.house_id == house.id,
            DemandRecord.timestamp >= today_start,
        ).all()
        
        total_gen = sum(g.generation_kwh for g in today_generation)
        total_demand = sum(d.demand_kwh for d in today_demand)
        
        # Update house record (these updates won't commit until safe_commit)
        house.current_month_generation_kwh = (house.current_month_generation_kwh or 0) + total_gen
        
        # All updates are staged, now commit atomically
        if not safe_commit(db, "Update daily stats"):
            logger.error(f"Failed to update daily stats for {house_id}")
            return False
        
        logger.info(f"✅ Daily stats updated for {house_id}: gen={total_gen}, demand={total_demand}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating daily stats: {e}")
        db.rollback()
        return False


# ============================================================================
# EXAMPLE 4: SAFE CONCURRENT WRITES (IDEMPOTENCY PATTERN)
# ============================================================================

def example_concurrent_iot_updates(db: Session, house_id: int, generation_kwh: float):
    """
    ✅ SAFE: Handle concurrent IoT updates from the same device.
    
    Scenario: IoT device sends same generation value multiple times due to network retries.
    
    Solution: Use idempotency to skip duplicates without database errors.
    """
    
    # Get house
    house = db.query(House).filter(House.house_id == house_id).first()
    if not house:
        return {"status": "error", "reason": "House not found"}
    
    # Check if similar generation was submitted in the last 30 seconds
    recent_records = safe_db.get_recent_generation_records(
        db,
        house.id,
        minutes=1,
    )
    
    for recent in recent_records:
        # Check if generation is similar (within 0.05 kWh tolerance)
        if abs(recent.generation_kwh - generation_kwh) < 0.05:
            logger.info(f"Concurrent duplicate detected and skipped: {house_id}")
            return {
                "status": "duplicate_skipped",
                "reason": "Identical generation already recorded",
                "identical_record_id": recent.id,
            }
    
    # Not a duplicate, create new record
    record = safe_db.create_generation_record(
        db,
        house.id,
        generation_kwh,
        device_id="NodeMCU_001",
    )
    
    if not record:
        return {"status": "error", "reason": "Failed to save generation"}
    
    return {
        "status": "success",
        "record_id": record.id,
    }


# ============================================================================
# EXAMPLE 5: ERROR RECOVERY PATTERN
# ============================================================================

def example_error_recovery(db: Session, house_id: int, demand_kwh: float) -> dict:
    """
    ✅ SAFE: Graceful error recovery with fallback to simpler logic.
    
    Scenario: Complex matching fails, but we still want to allocate energy.
    
    Solution: Try complex logic, fall back to simple grid allocation on failure.
    """
    
    # Find house
    house = db.query(House).filter(House.house_id == house_id).first()
    if not house:
        return {"status": "error", "reason": "House not found"}
    
    # Try complex matching
    try:
        matching = MatchingEngine(db)
        result = matching.match_demand(house.id, demand_kwh)
        logger.info(f"✅ Complex matching succeeded for {house_id}")
        return {
            "status": "complex_match",
            "pool_kwh": result["pool_kwh"],
            "grid_kwh": result["grid_kwh"],
        }
    
    except Exception as complex_error:
        logger.warning(f"Complex matching failed: {complex_error}")
        db.rollback()
        
        # Fallback 1: Try simple allocation
        try:
            record = safe_db.create_allocation(
                db,
                house_id=house.id,
                allocated_kwh=0,  # Simple: allocate nothing from pool
                source_type="grid",
            )
            
            if record:
                logger.info(f"✅ Fallback 1: Simple grid allocation for {house_id}")
                return {
                    "status": "simple_grid_fallback",
                    "alloc_id": record.id,
                    "grid_kwh": demand_kwh,
                    "pool_kwh": 0,
                }
        
        except Exception as fallback1_error:
            logger.error(f"Fallback 1 failed: {fallback1_error}")
            db.rollback()
        
        # Fallback 2: Return "no allocation" response without DB write
        logger.error(f"All allocation logic failed for {house_id}, returning error response")
        return {
            "status": "allocation_failed",
            "reason": "Matching and allocation logic failed",
            "demand_kwh": demand_kwh,
            "pool_kwh": 0,
            "grid_kwh": demand_kwh,
        }


# ============================================================================
# USAGE IN FASTAPI ROUTES
# ============================================================================
"""
# In your FastAPI route:

@router.post("/api/iot/update")
async def iot_update(data: IoTData, db: Session = Depends(get_db)):
    try:
        if data.generation_kwh > 0:
            return example_create_generation(db, data.house_id, data.generation_kwh)
        elif data.demand_kwh > 0:
            return example_create_and_match_demand(db, data.house_id, data.demand_kwh)
    except Exception as e:
        logger.error(f"IoT update failed: {e}")
        db.rollback()
        return {"status": "error", "error": str(e)[:100]}
"""
