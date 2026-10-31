from app.core.config import Settings
from app.worker.celery_app import build_celery_config, celery_app
from app.worker.tasks import ping


def test_ping_task_registered_and_runs() -> None:
    assert "system.ping" in celery_app.tasks
    assert ping.apply().get() == "pong"


def test_only_json_is_accepted() -> None:
    config = build_celery_config(Settings(_env_file=None))

    assert config["accept_content"] == ["json"]
    assert config["task_serializer"] == "json"


def test_visibility_timeout_exceeds_task_time_limit() -> None:
    # Otherwise Redis redelivers a still-running task and the document is processed twice.
    config = build_celery_config(Settings(_env_file=None, task_soft_time_limit_seconds=100))

    assert config["task_soft_time_limit"] == 100
    assert config["task_time_limit"] > 100
    assert config["broker_transport_options"]["visibility_timeout"] > config["task_time_limit"]


def test_tasks_survive_worker_loss() -> None:
    config = build_celery_config(Settings(_env_file=None))

    assert config["task_acks_late"] is True
    assert config["task_reject_on_worker_lost"] is True
    assert config["worker_prefetch_multiplier"] == 1
