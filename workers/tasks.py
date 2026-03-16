"""
tasks.py — Celery задачи
"""
from __future__ import annotations

import asyncio
import logging

from workers.celery_app import app

log = logging.getLogger(__name__)


def _run(coro):
    """Запускает async-корутину из синхронного Celery-воркера."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="workers.tasks.enrich_inn_task",
)
def enrich_inn_task(self, inn: str) -> dict:
    """
    Задача обогащения одного ИНН.
    Возвращает словарь с результатами (без записи в БД — для гибкости).
    """
    from pipeline.enrichment import enrich_one
    from pipeline.filters import compute_leasing_score
    from pipeline.scoring import score_company, get_priority

    try:
        result = _run(enrich_one(inn))

        # Считаем leasing_score если ещё не посчитан
        if "leasing_keyword_score" not in result:
            ls, kw = compute_leasing_score(result.get("leasing_texts", []))
            import json
            result["leasing_keyword_score"] = ls
            result["leasing_keywords_found"] = json.dumps(kw, ensure_ascii=False)

        score, triggers = score_company(result)
        result["score"]          = score
        result["priority"]       = get_priority(score)
        result["score_triggers"] = " | ".join(triggers)

        return result

    except Exception as exc:
        log.exception(f"enrich_inn_task {inn} failed: {exc}")
        raise self.retry(exc=exc)


@app.task(
    bind=True,
    name="workers.tasks.run_pipeline_task",
    soft_time_limit=3600,
)
def run_pipeline_task(
    self,
    category: str = "hosting",
    okveds: list | None = None,
    regions: list | None = None,
    output: str | None = None,
):
    """
    Запускает полный пайплайн как одну Celery задачу.
    Используй для запуска через CLI: celery call workers.tasks.run_pipeline_task
    """
    from main import run_full_pipeline
    _run(run_full_pipeline(category=category, okveds=okveds, regions=regions, output=output))
