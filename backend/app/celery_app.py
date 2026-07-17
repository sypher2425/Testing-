from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "video_dataset",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    worker_concurrency=settings.CELERY_CONCURRENCY,
    worker_max_tasks_per_child=10,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "cleanup-expired-jobs": {
            "task": "app.tasks.cleanup_expired_jobs",
            "schedule": crontab(minute=0),  # hourly
        },
        "reap-stale-jobs": {
            "task": "app.tasks.reap_stale_jobs",
            "schedule": crontab(minute="*/5"),
        },
    },
)
