"""Self-contained Celery async helper for v2 (no v1 import).

Runs each task on a fresh event loop and disposes the v2 engine afterwards.
"""
import asyncio
import functools
import logging
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


def async_to_sync(f: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., T]:
    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        except Exception as e:
            import traceback
            logger.error(f"Async task failed: {e}\n{traceback.format_exc()}")
            raise
        finally:
            try:
                from opencortyx.core.db import close_db
                loop.run_until_complete(close_db())
            except Exception as e:
                logger.warning(f"v2 DB cleanup error: {e}")
            loop.close()

    return wrapper
