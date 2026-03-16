"""
fns.py — проверка компании через сервисы ФНС:
  - egrul.nalog.ru   (статус, ОКВЭД, регион)
  - bo.nalog.ru      (ГИР БО: бухотчётность)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from config import (
    REQUEST_DELAY_MIN, REQUEST_DELAY_MAX,
    USER_AGENTS, PROXIES, MAX_RETRIES, REPORT_YEAR,
)

log = logging.getLogger(__name__)

GIRBO_BASE = "https://bo.nalog.ru/nbo"

ELECTRICITY_KW = [
    "электроэнерги", "электр. энерг", "э/э",
    "коммунальные услуги", "потребление энергии", "энергоносител",
]


def _rand_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer":         "https://bo.nalog.ru/",
    }


def _make_transport() -> httpx.AsyncHTTPTransport | None:
    if not PROXIES:
        return None
    return httpx.AsyncHTTPTransport(proxy=random.choice(PROXIES))


# ── ГИРБО ─────────────────────────────────────────────────────────────────

async def _girbo_search(client: httpx.AsyncClient, inn: str) -> list[dict]:
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                f"{GIRBO_BASE}/organizations/search",
                params={"query": inn, "page": 0, "size": 5},
                headers=_headers(),
                timeout=15,
            )
            r.raise_for_status()
            return r.json().get("content", [])
        except Exception as e:
            log.debug(f"ГИРБО search attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return []


async def _girbo_bfo_list(client: httpx.AsyncClient, org_id: int) -> list[dict]:
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                f"{GIRBO_BASE}/organizations/{org_id}/bfo",
                headers=_headers(),
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("content", [data])
        except Exception as e:
            log.debug(f"ГИРБО bfo_list attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return []


async def _girbo_bfo_detail(client: httpx.AsyncClient, bfo_id: int) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                f"{GIRBO_BASE}/bfo/{bfo_id}",
                headers=_headers(),
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.debug(f"ГИРБО bfo_detail attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return {}


def _parse_number(s: Any) -> float | None:
    s = re.sub(r"[\s\xa0]", "", str(s)).replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


FORM_CODES = {
    "2110": "revenue",
    "2120": "cost_of_sales",
    "1150": "fixed_assets",
    "1600": "balance_total",
    "2400": "net_profit",
}


def _extract_financials(report_detail: dict) -> dict[str, float]:
    fin: dict[str, float] = {v: 0.0 for v in FORM_CODES.values()}
    fin["employees"] = 0.0

    try:
        import json as _json
        report_str = _json.dumps(report_detail, ensure_ascii=False).lower()

        periods = report_detail if isinstance(report_detail, list) else [report_detail]
        for period in periods:
            if not isinstance(period, dict):
                continue
            for form in period.get("forms", [period]):
                if not isinstance(form, dict):
                    continue
                for row in form.get("rows", []):
                    code = str(row.get("code", "")).strip()
                    if code not in FORM_CODES:
                        continue
                    raw = row.get("currentPeriodValue") or row.get("value") or 0
                    try:
                        fin[FORM_CODES[code]] = abs(float(raw)) * 1000
                    except (TypeError, ValueError):
                        pass

            emp = period.get("averageNumberOfEmployees", 0)
            if emp:
                fin["employees"] = int(emp)

        # Расходы на ЭЭ — поиск по ключевым словам в JSON строке
        num_pat = r"[\d][\d\s\xa0]*(?:[.,][\d]+)?"
        ee_best = 0.0
        for kw in ELECTRICITY_KW:
            if kw.lower() not in report_str:
                continue
            for m in re.finditer(
                rf"{re.escape(kw.lower())}.{{0,150}}?({num_pat})", report_str
            ):
                v = _parse_number(m.group(1))
                if v and 100 < v * 1000 < 1e13:
                    ee_best = max(ee_best, v * 1000)
        fin["electricity"] = ee_best

    except Exception as e:
        log.debug(f"extract_financials error: {e}")

    return fin


async def fetch_fns(inn: str, year: int = REPORT_YEAR) -> dict[str, Any]:
    """
    Запрашивает ГИР БО для ИНН.
    Возвращает финансовые данные + налоговую нагрузку.
    """
    result: dict[str, Any] = {
        "inn":          inn,
        "org_name":     "",
        "region":       "",
        "okvd_main":    "",
        "status":       "",
        "is_active":    True,
        "revenue":      0.0,
        "cost_of_sales": 0.0,
        "fixed_assets": 0.0,
        "balance_total": 0.0,
        "net_profit":   0.0,
        "electricity":  0.0,
        "employees":    0,
        "tax_burden":   None,
        "error":        None,
    }

    transport = _make_transport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:
        await asyncio.sleep(_rand_delay())

        orgs = await _girbo_search(client, inn)
        if not orgs:
            result["error"] = "not_found_in_girbo"
            return result

        org    = orgs[0]
        org_id = org.get("id")
        if not org_id:
            result["error"] = "no_org_id"
            return result

        result["org_name"]  = org.get("shortName") or org.get("name", "")
        result["region"]    = org.get("region", "")
        result["okvd_main"] = org.get("okved", "")

        # Ликвидированная компания?
        status_raw = str(org.get("status", "")).lower()
        if any(kw in status_raw for kw in ("ликвид", "закрыт", "недейств", "прекра")):
            result["is_active"] = False
            result["status"] = org.get("status", "")

        await asyncio.sleep(_rand_delay())

        bfo_list = await _girbo_bfo_list(client, org_id)
        if not bfo_list:
            result["error"] = "no_bfo"
            return result

        # Берём нужный год или последний доступный
        target = next(
            (b for b in bfo_list
             if str(b.get("year") or b.get("reportYear") or "") == str(year)),
            bfo_list[0],
        )
        bfo_id = target.get("id")
        if not bfo_id:
            result["error"] = "no_bfo_id"
            return result

        await asyncio.sleep(_rand_delay())

        detail = await _girbo_bfo_detail(client, bfo_id)
        fin = _extract_financials(detail)

        result.update(fin)

    log.debug(
        f"ФНС {inn}: rev={result['revenue']:.0f}, FA={result['fixed_assets']:.0f}, "
        f"EE={result['electricity']:.0f}"
    )
    return result
