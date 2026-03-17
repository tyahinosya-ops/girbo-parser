"""
fedresurs_session.py  v6

httpx-сессионный подход:
  1. GET fedresurs.ru  → получаем cookies + XSRF-token
  2. Пробуем несколько вариантов search-эндпоинта с теми же куками
  3. Фильтруем ответы по ключевым словам локально

Запуск:
    python fedresurs_session.py
    python fedresurs_session.py --out results.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import random
import re
import sys
from datetime import date
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s – %(levelname)s – %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

BASE = "https://fedresurs.ru"

KEYWORDS: list[str] = [
    "antminer",
    "whatsminer",
    "bitmain",
    "microbt",
    "innosilicon",
    "asic майнер",
    "асик майнер",
    "майнинговое оборудование",
    "оборудование для майнинга",
]

PAGE_SIZE = 40
MAX_PAGES = 100  # ~4000 записей на ключевое слово

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Варианты search-эндпоинтов для перебора
SEARCH_ENDPOINTS = [
    ("GET",  "/backend/sfacts",          "searchString"),
    ("GET",  "/backend/sfacts",          "query"),
    ("GET",  "/backend/sfacts",          "q"),
    ("GET",  "/backend/search",          "searchString"),
    ("GET",  "/backend/search",          "query"),
    ("GET",  "/backend/messages",        "searchString"),
    ("GET",  "/backend/messages",        "query"),
    ("POST", "/backend/sfacts/search",   "searchString"),
    ("POST", "/backend/search/sfacts",   "searchString"),
    ("POST", "/backend/sfacts",          "searchString"),
]


def _extract_inn(text: str) -> str:
    m = re.search(r"\b(\d{10}|\d{12})\b", text or "")
    return m.group(1) if m else ""


def _base_headers(xsrf: str = "") -> dict:
    h = {
        "User-Agent":      UA,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer":         BASE + "/",
        "Origin":          BASE,
        "Connection":      "keep-alive",
    }
    if xsrf:
        h["X-XSRF-TOKEN"] = xsrf
        h["X-Requested-With"] = "XMLHttpRequest"
    return h


async def _init_session(client: httpx.AsyncClient) -> str:
    """Открывает главную страницу, получает куки и XSRF-токен."""
    try:
        r = await client.get(BASE + "/", timeout=20, headers={"User-Agent": UA})
        log.info(f"Главная: HTTP {r.status_code}, куки: {list(client.cookies.keys())}")
        xsrf = client.cookies.get("XSRF-TOKEN", "")
        return xsrf
    except Exception as e:
        log.warning(f"Ошибка init_session: {e}")
        return ""


def _detect_items(data: dict | list) -> tuple[list, int]:
    """Извлекает items и total из ответа с любой структурой."""
    if isinstance(data, list):
        return data, len(data)
    if not isinstance(data, dict):
        return [], 0
    # Ищем список среди всех значений
    for key, val in data.items():
        if isinstance(val, list) and len(val) > 0:
            # total — любое числовое поле с "total"/"count"/"size"
            total = 0
            for tkey in ("total", "totalCount", "totalElements", "count", "size"):
                if data.get(tkey):
                    total = int(data[tkey])
                    break
            if not total:
                total = len(val)
            return val, total
    # Нет данных, но может быть total > 0
    for tkey in ("total", "totalCount", "totalElements", "count", "size"):
        if data.get(tkey) and int(data[tkey]) > 0:
            return [], int(data[tkey])
    return [], 0


async def _probe_endpoint(
    client: httpx.AsyncClient, method: str, path: str, param: str, xsrf: str
) -> bool:
    """Пробует эндпоинт с probe-словом «лизинг». Возвращает True если получены данные."""
    import json as _json
    url = BASE + path
    try:
        if method == "GET":
            r = await client.get(
                url,
                params={"limit": 1, "offset": 0, param: "лизинг"},
                headers=_base_headers(xsrf),
                timeout=30,
            )
        else:
            r = await client.post(
                url,
                json={"limit": 1, "offset": 0, param: "лизинг"},
                headers={**_base_headers(xsrf), "Content-Type": "application/json"},
                timeout=30,
            )
        log.info(f"  probe {method} {path} ?{param}: HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            return False
        raw = r.text[:3000]
        log.info(f"  RAW: {raw}")   # ← видим структуру ответа в логах Actions
        try:
            data = r.json()
        except Exception:
            log.warning(f"  Не JSON: {raw[:200]}")
            return False
        items, total = _detect_items(data)
        has_data = len(items) > 0 or total > 0
        log.info(f"  → items={len(items)}, total={total}, has_data={has_data}")
        return has_data
    except Exception as e:
        log.info(f"  probe {method} {path}: {type(e).__name__}: {e}")
        return False


async def _fetch_page(
    client: httpx.AsyncClient,
    method: str, path: str, param: str, xsrf: str,
    keyword: str, offset: int,
) -> tuple[list[dict], int]:
    """Запрашивает одну страницу результатов. Возвращает (items, total)."""
    url = BASE + path
    for attempt in range(3):
        try:
            if method == "GET":
                r = await client.get(
                    url,
                    params={"limit": PAGE_SIZE, "offset": offset, param: keyword},
                    headers=_base_headers(xsrf),
                    timeout=20,
                )
            else:
                r = await client.post(
                    url,
                    json={"limit": PAGE_SIZE, "offset": offset, param: keyword},
                    headers={**_base_headers(xsrf), "Content-Type": "application/json"},
                    timeout=20,
                )
            if r.status_code == 429:
                wait = 30 + random.uniform(0, 15)
                log.warning(f"429 — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                return [], 0
            data = r.json()
            return _detect_items(data)
        except Exception as e:
            log.debug(f"fetch_page attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return [], 0


def _parse_msg(msg: dict, keyword: str) -> dict:
    def pick(*keys):
        for k in keys:
            v = msg.get(k)
            if v and str(v).strip().lower() not in ("null", "none", ""):
                return str(v).strip()
        return ""

    subject = pick(
        "subject", "contractSubject", "leaseSubject",
        "propertyDescription", "objectDescription",
        "description", "title", "text", "messageText",
    )

    lessee_inn = lessee_name = ""
    for k in ("lessee", "pledgor", "debtor", "client"):
        p = msg.get(k)
        if isinstance(p, dict):
            lessee_inn  = pick.__wrapped__(p, "inn", "code", "tin") if hasattr(pick, "__wrapped__") else (p.get("inn") or p.get("code") or "")
            lessee_name = (p.get("name") or p.get("fullName") or p.get("shortName") or "")
            if lessee_inn or lessee_name:
                break

    lessor_inn = lessor_name = ""
    for k in ("lessor", "pledgee", "creditor", "lender"):
        p = msg.get(k)
        if isinstance(p, dict):
            lessor_inn  = (p.get("inn") or p.get("code") or "")
            lessor_name = (p.get("name") or p.get("fullName") or p.get("shortName") or "")
            if lessor_inn or lessor_name:
                break

    if not lessee_inn:
        lessee_inn = _extract_inn(subject)

    msg_id   = str(msg.get("id") or msg.get("guid") or msg.get("messageId") or "")
    pub_date = str(msg.get("publishedDate") or msg.get("date") or msg.get("messageDate") or "")[:10]

    return {
        "keyword":     keyword,
        "lessee_inn":  str(lessee_inn),
        "lessee_name": str(lessee_name)[:200],
        "lessor_inn":  str(lessor_inn),
        "lessor_name": str(lessor_name)[:200],
        "date":        pub_date,
        "subject":     subject[:1000],
        "msg_id":      msg_id,
        "msg_url":     f"{BASE}/sfacts/{msg_id}" if msg_id else "",
    }


def _kw_match(msg: dict, keyword: str) -> bool:
    """Проверяет, упоминается ли ключевое слово в любом текстовом поле."""
    kw_lo = keyword.lower()
    text = " ".join(
        str(v) for v in msg.values()
        if isinstance(v, (str, int, float))
    ).lower()
    return kw_lo in text


async def search_keyword(
    client: httpx.AsyncClient,
    method: str, path: str, param: str, xsrf: str,
    keyword: str,
) -> list[dict]:
    results: list[dict] = []
    offset = 0
    for page_num in range(MAX_PAGES):
        items, total = await _fetch_page(client, method, path, param, xsrf, keyword, offset)
        if not items:
            break
        if page_num == 0:
            log.info(f"  «{keyword}»: total={total}")

        matched = [_parse_msg(m, keyword) for m in items if _kw_match(m, keyword)]
        results.extend(matched)

        for r in matched:
            log.info(
                f"    [{r['lessee_inn'] or '?':12}] "
                f"{r['lessee_name'] or '(без имени)':<35} | "
                f"{r['subject'][:55]}"
            )

        offset += PAGE_SIZE
        if total and offset >= total:
            break
        await asyncio.sleep(random.uniform(1.0, 2.5))

    log.info(f"  «{keyword}»: найдено {len(results)} совпадений")
    return results


async def main(out_path: str) -> None:
    all_records: list[dict] = []
    fields = [
        "keyword", "lessee_inn", "lessee_name",
        "lessor_inn", "lessor_name",
        "date", "subject", "msg_id", "msg_url",
    ]

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=3),
    ) as client:
        # 1. Получаем сессионные куки
        xsrf = await _init_session(client)

        # 2. Находим рабочий эндпоинт
        working: tuple | None = None
        log.info("Ищем рабочий search-эндпоинт ...")
        for method, path, param in SEARCH_ENDPOINTS:
            if await _probe_endpoint(client, method, path, param, xsrf):
                working = (method, path, param)
                log.info(f"✓ Рабочий эндпоинт: {method} {path} ?{param}=...")
                break
            await asyncio.sleep(1.0)

        if working is None:
            log.warning(
                "Ни один search-эндпоинт не вернул данные.\n"
                "Возможные причины: блокировка IP GitHub Actions, "
                "смена структуры API, таймаут.\n"
                "Проверьте строки RAW выше в логах."
            )
        else:
            method, path, param = working
            for kw in KEYWORDS:
                log.info(f"Ключевое слово: «{kw}»")
                recs = await search_keyword(client, method, path, param, xsrf, kw)
                all_records.extend(recs)
                await asyncio.sleep(random.uniform(2.0, 5.0))

    # Дедупликация
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        key = r["msg_id"] or (r["lessee_inn"] + r["date"] + r["keyword"] + r["subject"][:30])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # CSV
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(deduped)
    log.info(f"CSV: {out_path} ({len(deduped)} строк)")

    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)
    print("\n" + "=" * 60)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ  v6")
    print(f"  Рабочий эндпоинт:  {working or 'НЕ НАЙДЕН'}")
    print(f"  Найдено записей:   {len(deduped)}")
    print(f"  С ИНН:             {sum(1 for r in deduped if r['lessee_inn'])}")
    if kw_stats:
        print("\n  По ключевым словам:")
        for kw, cnt in kw_stats.most_common():
            print(f"    {kw:<45} {cnt}")
    print("=" * 60 + "\n")
    # всегда выходим с кодом 0 — CI не падает


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f"output/fedresurs_leasing_{date.today()}.csv")
    args = parser.parse_args()
    asyncio.run(main(args.out))
