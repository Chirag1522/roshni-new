"""
Safe async and timeout utilities for ROSHNI backend.
Prevents crashes and h11 protocol errors.
"""
import asyncio
import logging
from typing import Any, Callable, TypeVar, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def safe_execute(
    coro,
    timeout: float = 10.0,
    operation_name: str = "operation",
    fallback_value: Any = None,
) -> Any:
    """
    Execute an async operation with timeout and error handling.
    
    Args:
        coro: Coroutine to execute
        timeout: Timeout in seconds
        operation_name: Name of operation for logging
        fallback_value: Value to return if operation fails
        
    Returns:
        Result of operation or fallback_value on failure
    """
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        logger.debug(f"✅ {operation_name} completed successfully")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ {operation_name} timed out after {timeout}s")
        return fallback_value
    except Exception as e:
        logger.error(f"❌ {operation_name} failed: {str(e)}")
        return fallback_value


async def run_sync_in_thread(
    func: Callable[..., T],
    *args,
    timeout: float = 10.0,
    **kwargs,
) -> Optional[T]:
    """
    Run a synchronous function in a thread pool without blocking the event loop.
    
    Args:
        func: Synchronous function to run
        timeout: Timeout in seconds
        *args, **kwargs: Arguments to pass to func
        
    Returns:
        Result of func or None on timeout
    """
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: func(*args, **kwargs)),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(f"Sync operation timed out after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"Sync operation failed: {str(e)}")
        return None


def wrap_response(data: Any, status: str = "success", error: Optional[str] = None) -> dict:
    """
    Wrap response in consistent JSON format.
    Never raises an exception - always returns safe JSON.
    """
    return {
        "status": status,
        "error": error,
        "data": data,
    }


class SafeCache:
    """Simple in-memory cache with TTL."""
    
    def __init__(self):
        self._data = {}
        self._timestamps = {}
    
    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Store a value with TTL."""
        import time
        self._data[key] = value
        self._timestamps[key] = time.time() + ttl_seconds
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieve a value if it exists and hasn't expired."""
        import time
        if key not in self._data:
            return None
        
        if time.time() > self._timestamps.get(key, 0):
            # Expired
            del self._data[key]
            if key in self._timestamps:
                del self._timestamps[key]
            return None
        
        return self._data[key]
    
    def clear(self) -> None:
        """Clear all cache."""
        self._data.clear()
        self._timestamps.clear()


# Global cache instance
request_cache = SafeCache()
