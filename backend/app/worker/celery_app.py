from typing import Any

from celery import Celery
from celery.signals import setup_logging, worker_process_init

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine

HARD_LIMIT_GRACE_SECONDS = 120
VISIBILITY_TIMEOUT_MARGIN_SECONDS = 600
BROKER_CONNECT_TIMEOUT_SECONDS = 3
RECOVERY_INTERVAL_SECONDS = 300


def build_celery_config(settings: Settings) -> dict[str, Any]:
    hard_limit = settings.task_soft_time_limit_seconds + HARD_LIMIT_GRACE_SECONDS
    return {
        "broker_url": settings.redis_url,
        "result_backend": settings.redis_url,
        "broker_connection_retry_on_startup": True,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "result_expires": 86_400,
        "task_track_started": True,
        # A killed worker's task is redelivered, and each process holds one long OCR task at a
        # time rather than hoarding work that other workers could start.
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "worker_prefetch_multiplier": 1,
        "task_soft_time_limit": settings.task_soft_time_limit_seconds,
        "task_time_limit": hard_limit,
        # Redis redelivers unacked tasks after this timeout. With acks_late it must exceed the
        # longest task (a scanned 1000-page PDF), or the same document runs twice concurrently.
        "broker_transport_options": {
            "visibility_timeout": hard_limit + VISIBILITY_TIMEOUT_MARGIN_SECONDS,
            # So that an upload fails quickly, rather than hanging, if Redis is down.
            "socket_connect_timeout": BROKER_CONNECT_TIMEOUT_SECONDS,
        },
        "beat_schedule": {
            "recover-stalled-documents": {
                "task": "documents.recover_stalled",
                "schedule": RECOVERY_INTERVAL_SECONDS,
            }
        },
    }


celery_app = Celery("docintel", include=["app.worker.tasks"])
celery_app.conf.update(build_celery_config(get_settings()))


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    # Connecting to this signal also stops Celery from overriding the root logger.
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)


@worker_process_init.connect
def _reset_inherited_db_connections(**_: Any) -> None:
    # Pooled connections must not be shared across fork. close=False leaves the parent's intact.
    get_engine().dispose(close=False)
    if get_settings().preload_embedding_model:
        # Imported here because the parent process must not load the model before it forks.
        from app.search.embeddings import preload_embedder_in_background

        preload_embedder_in_background()
