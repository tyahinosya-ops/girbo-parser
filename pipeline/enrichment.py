"""
enrichment.py — Этап 2: параллельное обогащение по 3 источникам
  RusProfile (Playwright) + Федресурс (httpx) + ФНС/ГИР БО (httpx)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipeline.rusprofile import fetch_rusprofile_batch
from pipeline.fedresurs import fetch_fedresurs
from pipeline.fns import fetch_fns
from pipeline.notariat import fetch_notariat
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
        # ЕГРЮЛ (только из ФНС)
        "okveds_all":       fns.get("okveds_all", []),
        "director_name":    fns.get("director_name", ""),
        "reg_date":         fns.get("reg_date", ""),
        # Финансы — сначала ФНС, при 0 смотрим РусПрофайл
        "revenue":          _best("revenue", fns, rp) or 0.0,
        "cost_of_sales":    _best("cost_of_sales", fns, rp) or 0.0,
        "fixed_assets":     _best("fixed_assets", fns, rp) or 0.0,
        "balance_total":    _best("balance_total", fns, rp) or 0.0,
        "net_profit":       _best("net_profit", fns, rp) or 0.0,
        "employees":        _best("employees", fns, rp) or 0,
        "electricity":      _best("electricity", fns, rp) or 0.0,
        # Реестры ФНС
        "in_miner_registry":    fns.get("in_miner_registry", False),
        "in_infra_registry":    fns.get("in_infra_registry", False),
        "registry_check_error": fns.get("registry_check_error", False),
        # Залоги (notariat.ru — заполняется отдельным шагом)
        "notariat_pledges":     [],
        "has_asic_pledge":      False,
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



async def enrich_batch(inns: list[str]) -> list[dict[str, Any]]:
    """
    Обогащает список ИНН:
      - RusProfile: один браузер на весь батч (fetch_rusprofile_batch)
      - Федресурс + ФНС: параллельно по ENRICHMENT_CONCURRENCY
    """
    if not inns:
        return []

    # ── RusProfile: все ИНН через один браузер ──────────────────────────
    log.info(f"RusProfile: батч {len(inns)} ИНН")
    rp_list = await fetch_rusprofile_batch(inns)
    rp_map  = {r["inn"]: r for r in rp_list}

    # ── Федресурс + ФНС: параллельно ────────────────────────────────────
    semaphore = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)

    async def _fetch_all(inn: str) -> tuple[str, dict, dict, dict]:
        async with semaphore:
            fed_task      = asyncio.create_task(fetch_fedresurs(inn))
            fns_task      = asyncio.create_task(fetch_fns(inn))
            notariat_task = asyncio.create_task(fetch_notariat(inn))
            fed, fns, nor = await asyncio.gather(
                fed_task, fns_task, notariat_task, return_exceptions=True
            )
            if isinstance(fed, Exception):
                log.warning(f"Федресурс {inn}: {fed}")
                fed = {"inn": inn, "error": str(fed)}
            if isinstance(fns, Exception):
                log.warning(f"ФНС {inn}: {fns}")
                fns = {"inn": inn, "error": str(fns)}
            if isinstance(nor, Exception):
                log.warning(f"Нотариат {inn}: {nor}")
                nor = {"inn": inn, "pledges": [], "has_asic_pledge": False}
            return inn, fed, fns, nor

    tasks = [asyncio.create_task(_fetch_all(inn)) for inn in inns]
    results: list[dict[str, Any]] = []

    for coro in asyncio.as_completed(tasks):
        try:
            inn, fed, fns, nor = await coro
            rp  = rp_map.get(inn, {"inn": inn})
            res = _merge(inn, rp, fed, fns)
            # Проставляем залоги из нотариата
            res["notariat_pledges"] = nor.get("pledges", [])
            res["has_asic_pledge"]  = nor.get("has_asic_pledge", False)
            results.append(res)
            pledge_mark = " 🔒АСИК-залог" if res["has_asic_pledge"] else ""
            log.info(
                f"  [{len(results)}/{len(inns)}] {inn} — "
                f"rev={res.get('revenue', 0):.0f} "
                f"FA={res.get('fixed_assets', 0):.0f} "
                f"leasing={res.get('leasing_raw_count', 0)}"
                f"{pledge_mark}"
            )
        except Exception as e:
            log.error(f"enrich_batch error: {e}")

    return results
