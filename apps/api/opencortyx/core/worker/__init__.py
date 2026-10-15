"""The Celery application and the async bridge tasks run through.

    celery_app.py  the app itself: queues, routes, beat schedule, and ResilientTask (the
                   transient-contention retry every task inherits)
    utils.py       async_to_sync — tasks are sync callables around an async codebase

Deployment note: worker commands name this module path, so moving it again means updating
`compose.yaml` (four worker commands plus the healthcheck) in the same change.
"""
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync

__all__ = ["celery_app", "async_to_sync"]
