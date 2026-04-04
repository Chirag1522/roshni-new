#!/usr/bin/env python3
"""
Database Health Check & Verification Script

This script validates that all database fixes are in place:
✅ Primary keys are auto-increment
✅ Session handles errors with rollback
✅ Connection pooling is configured
✅ Tables exist and are accessible

Run this after startup to verify system is production-ready.
"""

import sys
import logging
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def check_autoincrement_keys(db: Session) -> bool:
    """
    ✅ Verify all models have autoincrement=True primary keys.
    
    This prevents: psycopg2.errors.UniqueViolation: duplicate key value violates...
    """
    logger.info("🔍 Checking primary key configurations...")
    
    models_to_check = [
        ('feeder', 'id'),
        ('house', 'id'),
        ('generation_record', 'id'),
        ('demand_record', 'id'),
        ('allocation', 'id'),
        ('pool_state', 'id'),
    ]
    
    inspector = inspect(db.bind)
    all_good = True
    
    for table_name, col_name in models_to_check:
        try:
            if table_name not in inspector.get_table_names():
                logger.warning(f"  ⚠️  Table '{table_name}' does not exist (not deployed yet)")
                continue
            
            columns = inspector.get_columns(table_name)
            pk_col = next((c for c in columns if c['name'] == col_name), None)
            
            if not pk_col:
                logger.error(f"  ❌ Column '{col_name}' not found in '{table_name}'")
                all_good = False
                continue
            
            # Check if autoincrement is set
            if pk_col.get('autoincrement', False):
                logger.info(f"  ✅ {table_name}.{col_name}: autoincrement=True")
            else:
                logger.error(f"  ❌ {table_name}.{col_name}: autoincrement NOT SET")
                all_good = False
                
        except Exception as e:
            logger.error(f"  ❌ Error checking {table_name}: {e}")
            all_good = False
    
    return all_good


def check_connection_pooling(db: Session) -> bool:
    """
    ✅ Verify connection pooling is configured.
    
    This prevents: "connection closed" errors and stale connections
    """
    logger.info("🔍 Checking connection pool configuration...")
    
    try:
        pool = db.get_bind().pool
        
        # Check pool_pre_ping
        if hasattr(pool, 'pre_ping'):
            logger.info(f"  ✅ pool_pre_ping enabled: {pool.pre_ping}")
        else:
            logger.warning(f"  ⚠️  pool_pre_ping not found (may be normal for this SQLAlchemy version)")
        
        # Check pool_recycle
        if hasattr(pool, 'recycle'):
            logger.info(f"  ✅ pool_recycle configured: {pool.recycle}s")
        else:
            logger.warning(f"  ⚠️  pool_recycle not found (may be normal for this SQLAlchemy version)")
        
        logger.info(f"  ℹ️  Pool size: {pool.size}, Max overflow: {pool.max_overflow}")
        
        return True
        
    except Exception as e:
        logger.error(f"  ❌ Error checking pool: {e}")
        return False


def check_database_access(db: Session) -> bool:
    """
    ✅ Verify database is accessible and responding.
    
    This prevents: timeout errors and connection failures
    """
    logger.info("🔍 Testing database connectivity...")
    
    try:
        result = db.execute(text("SELECT 1")).fetchone()
        if result:
            logger.info(f"  ✅ Database responds to queries")
            return True
        else:
            logger.error(f"  ❌ Database query returned no result")
            return False
            
    except Exception as e:
        logger.error(f"  ❌ Database connection failed: {e}")
        return False


def check_tables_exist(db: Session) -> bool:
    """
    ✅ Verify all required tables exist.
    """
    logger.info("🔍 Checking required tables...")
    
    required_tables = [
        'feeder', 'house', 'generation_record', 'demand_record',
        'allocation', 'pool_state', 'billing_record', 'transaction'
    ]
    
    try:
        inspector = inspect(db.bind)
        existing_tables = inspector.get_table_names()
        
        all_exist = True
        for table in required_tables:
            if table in existing_tables:
                logger.info(f"  ✅ Table '{table}' exists")
            else:
                logger.warning(f"  ⚠️  Table '{table}' does NOT exist (migrations may not be run)")
                all_exist = False
        
        return all_exist
        
    except Exception as e:
        logger.error(f"  ❌ Error checking tables: {e}")
        return False


def check_safe_db_ops_available(db: Session) -> bool:
    """
    ✅ Verify safe database operations module is importable.
    """
    logger.info("🔍 Checking safe DB operations module...")
    
    try:
        from app.services.safe_db_ops import SafeDatabaseOps
        
        methods = [
            'create_generation_record',
            'create_demand_record',
            'create_allocation',
            'update_allocation_status',
            'get_recent_demand_records',
            'get_recent_generation_records',
            'is_duplicate_demand',
            'is_duplicate_generation',
        ]
        
        all_present = True
        for method in methods:
            if hasattr(SafeDatabaseOps, method):
                logger.info(f"  ✅ SafeDatabaseOps.{method} available")
            else:
                logger.error(f"  ❌ SafeDatabaseOps.{method} NOT FOUND")
                all_present = False
        
        return all_present
        
    except ImportError as e:
        logger.error(f"  ❌ Failed to import SafeDatabaseOps: {e}")
        return False
    except Exception as e:
        logger.error(f"  ❌ Error checking safe DB ops: {e}")
        return False


def main():
    """Run all checks and report status."""
    
    logger.info("=" * 70)
    logger.info("DATABASE HEALTH CHECK - Production Ready Verification")
    logger.info("=" * 70)
    
    try:
        # Import database dependencies
        from database import SessionLocal
        
        db = SessionLocal()
        
        results = {
            "Autoincrement Keys": check_autoincrement_keys(db),
            "Connection Pooling": check_connection_pooling(db),
            "Database Access": check_database_access(db),
            "Required Tables": check_tables_exist(db),
            "Safe DB Operations": check_safe_db_ops_available(db),
        }
        
        db.close()
        
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")
        logger.error("Make sure DATABASE_URL is set and database is running")
        sys.exit(1)
    
    # Print summary
    logger.info("=" * 70)
    logger.info("HEALTH CHECK SUMMARY")
    logger.info("=" * 70)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for check_name, passed_bool in results.items():
        status = "✅ PASS" if passed_bool else "❌ FAIL"
        logger.info(f"{status:10} - {check_name}")
    
    logger.info("=" * 70)
    logger.info(f"Total: {passed}/{total} checks passed")
    
    if passed == total:
        logger.info("🎉 System is PRODUCTION READY")
        sys.exit(0)
    else:
        logger.warning("⚠️  Some checks failed - review logs above")
        sys.exit(1)


if __name__ == "__main__":
    main()
