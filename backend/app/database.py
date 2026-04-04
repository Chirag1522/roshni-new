"""
Database configuration and session management.
✅ Production-safe with proper error handling and rollback
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
from typing import Generator, Optional, TypeVar, Callable
import logging

from config import settings

logger = logging.getLogger(__name__)

# Create database engine
engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    connect_args={
        "check_same_thread": False,
        "connect_timeout": 10,  # ✅ 10 second connection timeout
    } if "sqlite" in settings.database_url else {
        "connect_timeout": 10,  # ✅ 10 second connection timeout for PostgreSQL
    },
    pool_pre_ping=True,       # ✅ Test connections before using them
    pool_recycle=3600,        # ✅ Recycle connections every hour
    pool_size=10,             # ✅ Number of connections to keep open
    max_overflow=10,          # ✅ Number of overflow connections allowed
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()

def init_db():
    """Initialize database tables."""
    logger.info("Initializing database...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Database initialized successfully")
        # Test connection
        test_connection()
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")
        raise

def test_connection():
    """Test database connection and log status."""
    try:
        db = SessionLocal()
        db.execute("SELECT 1")
        db.close()
        logger.info("✅ Database connection test PASSED")
    except Exception as e:
        logger.error(f"❌ Database connection test FAILED: {e}")
        logger.error(f"   DATABASE_URL: {settings.database_url[:50]}...")
        logger.error(f"   Check that:")
        logger.error(f"   1. Database hostname is reachable")
        logger.error(f"   2. Credentials are correct (user/password)")
        logger.error(f"   3. Port is specified (usually 5432)")
        logger.error(f"   4. SSL mode is configured if required")
        raise


def get_db() -> Generator:
    """
    Dependency for getting database session.
    ✅ Safely handles rollback after exceptions.
    """
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database error: {str(e)}")
        db.rollback()  # ✅ CRITICAL: Rollback on error
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        db.rollback()  # ✅ CRITICAL: Rollback on any error
        raise
    finally:
        db.close()  # ✅ Always close, even if error


T = TypeVar("T")


def safe_db_operation(db: Session, operation: Callable[[], T], operation_name: str = "DB operation") -> Optional[T]:
    """
    Execute a database operation safely with automatic rollback.
    
    Usage:
        def add_record():
            record = GenerationRecord(...)
            db.add(record)
            db.commit()
            return record
        
        result = safe_db_operation(db, add_record, "Add record")
    
    Returns:
        Result of operation or None if failed
    """
    try:
        result = operation()
        logger.debug(f"✅ {operation_name} succeeded")
        return result
    except SQLAlchemyError as e:
        logger.error(f"❌ {operation_name} failed (SQLAlchemy): {str(e)}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.error(f"❌ {operation_name} failed (unexpected): {str(e)}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def safe_commit(db: Session, operation_name: str = "Commit") -> bool:
    """
    Safely commit a transaction with rollback on failure.
    
    Usage:
        db.add(obj)
        if not safe_commit(db, "Add generation record"):
            return error_response
    
    Returns:
        True if successful, False if failed
    """
    try:
        db.commit()
        logger.debug(f"✅ {operation_name} committed")
        return True
    except SQLAlchemyError as e:
        logger.error(f"❌ {operation_name} commit failed: {str(e)}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error(f"❌ {operation_name} unexpected error: {str(e)}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
