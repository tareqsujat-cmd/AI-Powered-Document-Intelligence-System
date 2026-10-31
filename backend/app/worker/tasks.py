from app.worker.celery_app import celery_app


@celery_app.task(name="system.ping")
def ping() -> str:
    """Confirms a worker can receive and run a task."""
    return "pong"
