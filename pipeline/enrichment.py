"""
enrichment.py — Этап 2: параллельное обогащение по 3 источникам
  RusProfile (Playwright) + Федресурс (httpx) + ФНС/ГИР БО (httpx)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipeline.rusprofile import fetch_rusprofile
from pipeline.fedresurs import fetch_fedresurs
from pipeline.fns import fetch_fns
from config import ENRICHMENT_CONCURRENCY

log = logging.getLogger(__name__)


def _merge(inn: str, rp: dict, fed: dict, fns: dict) -> dict[str, Any]:
    """
    Объединяет данные из трёх источников в единую запись.
    Приоритет: ФНС > РусПрофайл (финансы более надёжны у ФНС).
    """
    def _best(key: str, *dicts) -> Any:
        """Берёт первое ненулевое значение."""
        for d in dicts:
            v = d.get(key)
            if v is not None and v != 0 and v != "" and v != 0.0:
                return v
        return dicts[-1].get(key, None)

    return {
        "inn":              inn,
        # Мета
        "name":             _best("org_name", fns) or _best("name", rp) or "",
        "region":           _best("region", fns, rp),
        "okvd_main":        _best("okvd_main", fns),
        "is_active":        fns.get("is_active", True),
        "status":           _best("status", fns, rp),
        # Финансы — сначала ФНС, при 0 смотрим РусПрофайл
        "revenue":          _best("revenue", fns, rp) or 0.0,
        "cost_of_sales":    _best("cost_of_sales", fns, rp) or 0.0,
        "fixed_assets":     _best("fixed_assets", fns, rp) or 0.0,
        "balance_total":    _best("balance_total", fns, rp) or 0.0,
        "net_profit":       _best("net_profit", fns, rp) or 0.0,
        "employees":        _best("employees", fns, rp) or 0,
        "electricity":      _best("electricity", fns, rp) or 0.0,
        # Федресурс
        "leasing_texts":    fed.get("leasing_texts", []),
        "leasing_raw_count": fed.get("raw_count", 0),
        # Ошибки
        "errors": {
            "rusprofile":  rp.get("error"),
            "fedresurs":   fed.get("error"),
            "fns":         fns.get("error"),
        },
    }


async def enrich_one(inn: str) -> dict[str, Any]:
    """
    Обогащает один ИНН параллельно из всех трёх источников.
    """
    log.info(f"Обогащение ИНН: {inn}")

    # Запускаем все три воркера одновременно
    rp_task  = asyncio.create_task(fetch_rusprofile(inn))
    fed_task = asyncio.create_task(fetch_fedresurs(inn))
    fns_task = asyncio.create_task(fetch_fns(inn))

    rp, fed, fns = await asyncio.gather(rp_task, fed_task, fns_task, return_exceptions=True)

    # Приводим к dict если вдруг вылетело исключение
    if isinstance(rp, Exception):
        log.warning(f"RusProfile {inn} exception: {rp}")
        rp = {"inn": inn, "error": str(rp)}
    if isinstance(fed, Exception):
        log.warning(f"Федресурс {inn} exception: {fed}")
        fed = {"inn": inn, "error": str(fed)}
    if isinstance(fns, Exception):
        log.warning(f"ФНС {inn} exception: {fns}")
        fns = {"inn": inn, "error": str(fns)}

    return _merge(inn, rp, fed, fns)


async def enrich_batch(inns: list[str]) -> list[dict[str, Any]]:
    """
    Обогащает список ИНН с ограничением параллелизма.
    ENRICHMENT_CONCURRENCY — сколько ИНН обрабатываем одновременно.
    """
    semaphore = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)
    results: list[dict[str, Any]] = []

    async def _worker(inn: str) -> dict[str, Any]:
        async with semaphore:
            return await enrich_one(inn)

    tasks = [asyncio.create_task(_worker(inn)) for inn in inns]

    for coro in asyncio.as_completed(tasks):
        try:
            res = await coro
            results.append(res)
            log.info(
                f"  [{len(results)}/{len(inns)}] {res['inn']} — "
                f"rev={res.get('revenue', 0):.0f}, "
                f"FA={res.get('fixed_assets', 0):.0f}, "
                f"leasing_msgs={res.get('leasing_raw_count', 0)}"
            )
        except Exception as e:
            log.error(f"enrich_batch error: {e}")

    return results
