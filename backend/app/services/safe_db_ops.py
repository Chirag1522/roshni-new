"""
Safe database operation helpers.
✅ All operations have error handling and proper rollback.
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from datetime import datetime
from typing import Optional, List

from app.models import GenerationRecord, DemandRecord, Allocation, House
from config import settings

logger = logging.getLogger(__name__)


class SafeDatabaseOps:
    """Safe database operations with automatic error handling."""

    @staticmethod
    def create_generation_record(
        db: Session,
        house_id: int,
        generation_kwh: float,
        device_id: str = None,
        signal_strength: int = None,
    ) -> Optional[GenerationRecord]:
        """
        Safely create a generation record.
        
        Returns:
            GenerationRecord if successful, None if failed
        """
        try:
            # ✅ IMPORTANT: Never manually set ID - let autoincrement handle it
            record = GenerationRecord(
                house_id=house_id,
                generation_kwh=generation_kwh,
                device_id=device_id,
                signal_strength=signal_strength,
                timestamp=datetime.utcnow(),
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            logger.info(f"✅ Generation record created: ID={record.id}")
            return record
        except IntegrityError as e:
            logger.error(f"Integrity error (duplicate key): {str(e)}")
            db.rollback()
            return None
        except SQLAlchemyError as e:
            logger.error(f"Database error: {str(e)}")
            db.rollback()
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            db.rollback()
            return None

    @staticmethod
    def create_demand_record(
        db: Session,
        house_id: int,
        demand_kwh: float,
        priority_level: int = 5,
        duration_hours: float = 1.0,
    ) -> Optional[DemandRecord]:
        """
        Safely create a demand record.
        
        Returns:
            DemandRecord if successful, None if failed
        """
        try:
            # ✅ IMPORTANT: Never manually set ID - let autoincrement handle it
            demand = DemandRecord(
                house_id=house_id,
                demand_kwh=demand_kwh,
                priority_level=priority_level,
                duration_hours=duration_hours,
                status="pending",
                timestamp=datetime.utcnow(),
            )
            db.add(demand)
            db.commit()
            db.refresh(demand)
            logger.info(f"✅ Demand record created: ID={demand.id}")
            return demand
        except IntegrityError as e:
            logger.error(f"Integrity error (duplicate key): {str(e)}")
            db.rollback()
            return None
        except SQLAlchemyError as e:
            logger.error(f"Database error: {str(e)}")
            db.rollback()
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            db.rollback()
            return None

    @staticmethod
    def create_allocation(
        db: Session,
        house_id: int,
        allocated_kwh: float,
        source_type: str = "pool",
        ai_reasoning: str = None,
    ) -> Optional[Allocation]:
        """
        Safely create an allocation record.
        
        Returns:
            Allocation if successful, None if failed
        """
        try:
            # ✅ IMPORTANT: Never manually set ID - let autoincrement handle it
            allocation = Allocation(
                house_id=house_id,
                allocated_kwh=allocated_kwh,
                source_type=source_type,
                status="completed",
                ai_reasoning=ai_reasoning,
            )
            db.add(allocation)
            db.commit()
            db.refresh(allocation)
            logger.info(f"✅ Allocation created: ID={allocation.id}")
            return allocation
        except IntegrityError as e:
            logger.error(f"Integrity error (duplicate key): {str(e)}")
            db.rollback()
            return None
        except SQLAlchemyError as e:
            logger.error(f"Database error: {str(e)}")
            db.rollback()
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            db.rollback()
            return None

    @staticmethod
    def update_allocation_status(
        db: Session,
        allocation_id: int,
        status: str,
        transaction_hash: str = None,
    ) -> bool:
        """
        Safely update allocation status.
        
        Returns:
            True if successful, False if failed
        """
        try:
            allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
            if not allocation:
                logger.warning(f"Allocation {allocation_id} not found")
                return False
            
            allocation.status = status
            if transaction_hash:
                allocation.transaction_hash = transaction_hash
            
            db.commit()
            logger.info(f"✅ Allocation {allocation_id} status updated to {status}")
            return True
        except SQLAlchemyError as e:
            logger.error(f"Database error updating allocation: {str(e)}")
            db.rollback()
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating allocation: {str(e)}")
            db.rollback()
            return False

    @staticmethod
    def get_recent_demand_records(
        db: Session,
        house_id: int,
        minutes: int = 5,
    ) -> List[DemandRecord]:
        """
        Get demand records from the last N minutes.
        Useful for avoiding duplicate submission processing.
        
        Returns:
            List of DemandRecord objects
        """
        try:
            from datetime import timedelta
            time_threshold = datetime.utcnow() - timedelta(minutes=minutes)
            
            records = db.query(DemandRecord).filter(
                DemandRecord.house_id == house_id,
                DemandRecord.timestamp >= time_threshold,
            ).order_by(DemandRecord.timestamp.desc()).all()
            
            return records
        except SQLAlchemyError as e:
            logger.error(f"Error fetching demand records: {str(e)}")
            return []

    @staticmethod
    def get_recent_generation_records(
        db: Session,
        house_id: int,
        minutes: int = 1,
    ) -> List[GenerationRecord]:
        """
        Get generation records from the last N minutes.
        Useful for avoiding duplicate generation processing.
        
        Returns:
            List of GenerationRecord objects
        """
        try:
            from datetime import timedelta
            time_threshold = datetime.utcnow() - timedelta(minutes=minutes)
            
            records = db.query(GenerationRecord).filter(
                GenerationRecord.house_id == house_id,
                GenerationRecord.timestamp >= time_threshold,
            ).order_by(GenerationRecord.timestamp.desc()).all()
            
            return records
        except SQLAlchemyError as e:
            logger.error(f"Error fetching generation records: {str(e)}")
            return []

    @staticmethod
    def is_duplicate_demand(
        db: Session,
        house_id: int,
        demand_kwh: float,
        minutes: int = 1,
        tolerance: float = 0.1,
    ) -> bool:
        """
        Check if similar demand was recently submitted.
        Uses value comparison with tolerance (default 0.1 kWh).
        
        Args:
            house_id: House database ID
            demand_kwh: Demand value to check
            minutes: Look back this many minutes
            tolerance: Tolerance for value comparison (e.g., 0.1 means ±0.1 kWh)
        
        Returns:
            True if duplicate found, False otherwise
        """
        try:
            from datetime import timedelta
            time_threshold = datetime.utcnow() - timedelta(minutes=minutes)
            
            recent = db.query(DemandRecord).filter(
                DemandRecord.house_id == house_id,
                DemandRecord.timestamp >= time_threshold,
            ).all()
            
            for record in recent:
                if abs(record.demand_kwh - demand_kwh) < tolerance:
                    logger.warning(
                        f"Duplicate demand detected: "
                        f"existing={record.demand_kwh:.2f}, new={demand_kwh:.2f}"
                    )
                    return True
            
            return False
        except SQLAlchemyError as e:
            logger.error(f"Error checking duplicate demand: {str(e)}")
            return False

    @staticmethod
    def is_duplicate_generation(
        db: Session,
        house_id: int,
        generation_kwh: float,
        seconds: int = 10,
        tolerance: float = 0.1,
    ) -> bool:
        """
        Check if similar generation was recently submitted.
        Uses value comparison with tolerance (default 0.1 kWh).
        
        Args:
            house_id: House database ID
            generation_kwh: Generation value to check
            seconds: Look back this many seconds
            tolerance: Tolerance for value comparison
        
        Returns:
            True if duplicate found, False otherwise
        """
        try:
            from datetime import timedelta
            time_threshold = datetime.utcnow() - timedelta(seconds=seconds)
            
            recent = db.query(GenerationRecord).filter(
                GenerationRecord.house_id == house_id,
                GenerationRecord.timestamp >= time_threshold,
            ).all()
            
            for record in recent:
                if abs(record.generation_kwh - generation_kwh) < tolerance:
                    logger.warning(
                        f"Duplicate generation detected: "
                        f"existing={record.generation_kwh:.2f}, new={generation_kwh:.2f}"
                    )
                    return True
            
            return False
        except SQLAlchemyError as e:
            logger.error(f"Error checking duplicate generation: {str(e)}")
            return False


# Global instance for easy access
safe_db = SafeDatabaseOps()
