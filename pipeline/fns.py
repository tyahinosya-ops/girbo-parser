"""
fns.py — проверка компании через сервисы ФНС:
  - bo.nalog.ru      (ГИР БО: бухотчётность)
  - egrul.nalog.ru   (все ОКВЭДы, руководитель, дата регистрации)
  - service.nalog.ru (реестры майнеров ФНС, Закон №259-ФЗ)
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

GIRBO_BASE  = "https://bo.nalog.ru/nbo"
EGRUL_BASE  = "https://egrul.nalog.ru"

# ⚠️ Реестры майнеров ФНС (Закон №259-ФЗ, 2024)
# URL уточнить когда ФНС откроет публичный API — сейчас при 404 просто False
FNS_MINER_REGISTRY_URL = "https://service.nalog.ru/miner/api/v1/check"
FNS_INFRA_REGISTRY_URL = "https://service.nalog.ru/miner/api/v1/infra/check"

# Circuit breaker: если ГИРБО вернул 403/недоступен — обходим его для
# всех следующих ИНН в текущем запуске, чтобы не тратить время попусту.
_girbo_circuit_open: bool = False


def _open_girbo_circuit(reason: str) -> None:
    global _girbo_circuit_open
    if not _girbo_circuit_open:
        log.warning(f"ГИРБО circuit breaker OPEN: {reason} — пропускаем ГИРБО для всех ИНН")
        _girbo_circuit_open = True

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
            # Не ретраим: 4xx не исправятся повтором
            if r.status_code in (400, 403, 404):
                _open_girbo_circuit(f"HTTP {r.status_code}")
                log.debug(f"ГИРБО search {inn}: HTTP {r.status_code} — fast-fail")
                return []
            r.raise_for_status()
            return r.json().get("content", [])
        except httpx.HTTPStatusError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _open_girbo_circuit(str(e))
            return []
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
            if r.status_code in (400, 403, 404):
                log.debug(f"ГИРБО bfo_list org={org_id}: HTTP {r.status_code} — fast-fail")
                return []
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("content", [data])
        except httpx.HTTPStatusError:
            raise
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
            if r.status_code in (400, 403, 404):
                log.debug(f"ГИРБО bfo_detail bfo={bfo_id}: HTTP {r.status_code} — fast-fail")
                return {}
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError:
            raise
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


# ── ЕГРЮЛ ─────────────────────────────────────────────────────────────────

async def _egrul_fetch(client: httpx.AsyncClient, inn: str) -> dict[str, Any]:
    """
    Запрашивает ЕГРЮЛ и возвращает:
      okvd_main, okveds_all, org_name, region, status,
      is_active, director_name, reg_date
    """
    out: dict[str, Any] = {
        "org_name":      "",
        "region":        "",
        "status":        "",
        "is_active":     True,
        "okvd_main":     "",
        "okveds_all":    [],
        "director_name": "",
        "reg_date":      "",
    }
    try:
        # Шаг 1: получаем токен поиска
        egrul_headers = {**_headers(), "Referer": "https://egrul.nalog.ru/"}
        token_r = await client.post(
            f"{EGRUL_BASE}/",
            data={"query": inn, "region": "0", "PreventChromeAutocomplete": ""},
            headers={**egrul_headers, "X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        token_r.raise_for_status()
        token = token_r.json().get("t")
        if not token:
            return out

        await asyncio.sleep(random.uniform(1.0, 2.5))

        # Шаг 2: список организаций
        search_r = await client.get(
            f"{EGRUL_BASE}/search-result/{token}",
            headers=egrul_headers,
            timeout=15,
        )
        search_r.raise_for_status()
        rows = search_r.json().get("rows", [])
        if not rows:
            return out

        org = rows[0]
        out["org_name"] = org.get("n", "")
        out["okvd_main"] = org.get("o", "")

        addr = org.get("a", {})
        out["region"] = addr.get("r", "") if isinstance(addr, dict) else ""

        reg = org.get("r", {})
        out["reg_date"] = reg.get("r", "") if isinstance(reg, dict) else ""

        status_raw = str(org.get("s", "")).lower()
        out["status"] = org.get("s", "")
        if any(kw in status_raw for kw in ("ликвид", "закрыт", "недейств", "прекра")):
            out["is_active"] = False

        # Шаг 3: полная выписка (все ОКВЭДы + руководитель)
        req_token = org.get("t")
        if not req_token:
            return out

        await asyncio.sleep(random.uniform(1.5, 3.0))
        detail_r = await client.get(
            f"{EGRUL_BASE}/vyp-short-result/{req_token}",
            headers=egrul_headers,
            timeout=20,
        )
        detail_r.raise_for_status()
        detail = detail_r.json()

        okveds: list[str] = []

        # Основной ОКВЭД
        main_okved = detail.get("СвОКВЭД", {}).get("СвОКВЭДОсн", {})
        if isinstance(main_okved, dict):
            code = main_okved.get("КодОКВЭД", "")
            if code:
                okveds.append(code)
                out["okvd_main"] = out["okvd_main"] or code

        # Дополнительные ОКВЭДы
        extra = detail.get("СвОКВЭД", {}).get("СвОКВЭДДоп", [])
        if isinstance(extra, dict):
            extra = [extra]
        for item in extra:
            code = item.get("КодОКВЭД", "")
            if code and code not in okveds:
                okveds.append(code)

        out["okveds_all"] = okveds

        # Руководитель
        director = detail.get("СвРуководит", {})
        if isinstance(director, dict):
            fio = director.get("ФИОРуководит", {})
            if isinstance(fio, dict):
                parts = [fio.get("Фамилия", ""), fio.get("Имя", ""), fio.get("Отчество", "")]
                out["director_name"] = " ".join(p for p in parts if p)

    except Exception as e:
        log.debug(f"ЕГРЮЛ {inn}: {e}")

    return out


# ── Реестры майнеров ФНС ───────────────────────────────────────────────────

async def _check_fns_registries(
    client: httpx.AsyncClient, inn: str
) -> dict[str, bool]:
    """
    Проверяет ИНН в реестрах майнеров ФНС (Закон №259-ФЗ).
    При недоступности API возвращает False без ошибки.
    """
    result = {
        "in_miner_registry": False,
        "in_infra_registry": False,
        "registry_check_error": False,
    }
    for key, url in (
        ("in_miner_registry", FNS_MINER_REGISTRY_URL),
        ("in_infra_registry", FNS_INFRA_REGISTRY_URL),
    ):
        try:
            r = await client.get(
                url, params={"inn": inn}, headers=_headers(), timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                result[key] = bool(
                    data.get("found") or data.get("inRegistry") or data.get("exists")
                )
            elif r.status_code != 404:
                result["registry_check_error"] = True
        except Exception as e:
            log.debug(f"Реестр ФНС ({key}) {inn}: {e}")
            result["registry_check_error"] = True
        await asyncio.sleep(random.uniform(0.5, 1.5))

    return result


# ── Основная функция ───────────────────────────────────────────────────────

async def fetch_fns(inn: str, year: int = REPORT_YEAR) -> dict[str, Any]:
    """
    Полное обогащение от ФНС:
      1. ГИРБО  — финансовая отчётность (bo.nalog.ru)
      2. ЕГРЮЛ  — все ОКВЭДы, руководитель, дата регистрации
      3. Реестры майнеров ФНС (Закон №259-ФЗ)
    """
    result: dict[str, Any] = {
        # идентификация
        "inn":           inn,
        "org_name":      "",
        "region":        "",
        "status":        "",
        "is_active":     True,
        "okvd_main":     "",
        # финансы (ГИРБО)
        "revenue":       0.0,
        "cost_of_sales": 0.0,
        "fixed_assets":  0.0,
        "balance_total": 0.0,
        "net_profit":    0.0,
        "electricity":   0.0,
        "employees":     0,
        "tax_burden":    None,
        # ЕГРЮЛ
        "okveds_all":    [],
        "director_name": "",
        "reg_date":      "",
        # Реестры ФНС
        "in_miner_registry":    False,
        "in_infra_registry":    False,
        "registry_check_error": False,
        # служебное
        "error": None,
    }

    transport = _make_transport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:

        # ── 1. ГИРБО ──────────────────────────────────────────────────────
        if not _girbo_circuit_open:
            await asyncio.sleep(_rand_delay())
            orgs = await _girbo_search(client, inn)

            if orgs:
                org    = orgs[0]
                org_id = org.get("id")

                result["org_name"]  = org.get("shortName") or org.get("name", "")
                result["region"]    = org.get("region", "")
                result["okvd_main"] = org.get("okved", "")

                status_raw = str(org.get("status", "")).lower()
                if any(kw in status_raw for kw in ("ликвид", "закрыт", "недейств", "прекра")):
                    result["is_active"] = False
                result["status"] = org.get("status", "")

                if org_id:
                    await asyncio.sleep(_rand_delay())
                    bfo_list = await _girbo_bfo_list(client, org_id)
                    if bfo_list:
                        target = next(
                            (b for b in bfo_list
                             if str(b.get("year") or b.get("reportYear") or "") == str(year)),
                            bfo_list[0],
                        )
                        bfo_id = target.get("id")
                        if bfo_id:
                            await asyncio.sleep(_rand_delay())
                            detail = await _girbo_bfo_detail(client, bfo_id)
                            result.update(_extract_financials(detail))
            else:
                result["error"] = "not_found_in_girbo"
        else:
            result["error"] = "girbo_circuit_open"

        # ── 2. ЕГРЮЛ — все ОКВЭДы, руководитель, дата регистрации ────────
        await asyncio.sleep(_rand_delay())
        egrul = await _egrul_fetch(client, inn)

        # Заполняем только если ГИРБО не дал данных
        if not result["org_name"]:
            result["org_name"]  = egrul["org_name"]
        if not result["region"]:
            result["region"]    = egrul["region"]
        if not result["okvd_main"]:
            result["okvd_main"] = egrul["okvd_main"]
        if not result["status"]:
            result["status"]    = egrul["status"]
            result["is_active"] = egrul["is_active"]

        result["okveds_all"]    = egrul["okveds_all"]
        result["director_name"] = egrul["director_name"]
        result["reg_date"]      = egrul["reg_date"]

        # ── 3. Реестры майнеров ────────────────────────────────────────────
        await asyncio.sleep(_rand_delay())
        result.update(await _check_fns_registries(client, inn))

    log.debug(
        f"ФНС {inn}: rev={result['revenue']:.0f} FA={result['fixed_assets']:.0f} "
        f"EE={result['electricity']:.0f} okveds={result['okveds_all']} "
        f"miner_reg={result['in_miner_registry']}"
    )
    return result
