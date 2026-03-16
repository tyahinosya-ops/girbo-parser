"""
celery_app.py — настройка Celery + Redis
"""
from celery import Celery
from config import CELERY_BROKER, CELERY_BACKEND

app = Celery(
    "mining_parser",
    broker=CELERY_BROKER,
    backend=CELERY_BACKEND,
    include=["workers.tasks"],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Moscow",
    enable_utc=True,
    task_acks_late=True,                # подтверждаем после выполнения
    worker_prefetch_multiplier=1,       # по одной задаче на воркер
    task_soft_time_limit=600,           # 10 минут мягкий лимит
    task_time_limit=720,                # 12 минут жёсткий
    task_routes={
        "workers.tasks.enrich_inn_task": {"queue": "enrich"},
        "workers.tasks.run_pipeline_task": {"queue": "pipeline"},
    },
)
