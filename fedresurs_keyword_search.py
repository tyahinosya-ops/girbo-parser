"""
fedresurs_keyword_search.py

Прямой поиск на Федресурсе по ключевым словам (лизинг майнинг-оборудования).
Не требует ЕГРЮЛ. Сохраняет CSV со всеми лизингополучателями.

Запуск:
    python fedresurs_keyword_search.py
    python fedresurs_keyword_search.py --out my_results.csv --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

# ── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s – %(levelname)s – %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Ключевые слова поиска ──────────────────────────────────────────────────
SEARCH_KEYWORDS: list[str] = [
    "antminer",
    "whatsminer",
    "bitmain",
    "microbt",
    "асик майнер",
    "asic майнер",
    "майнинг оборудование лизинг",
    "криптомайнер лизинг",
    "эвм лизинг",
    "сервер лизинг майнинг",
]

# ── Эндпоинты Федресурса (пробуем по порядку) ─────────────────────────────
ENDPOINTS: list[str] = [
    "https://fedresurs.ru/backend/efrs-messages",
    "https://fedresurs.ru/backend/search",
    "https://fedresurs.ru/api/messages",
]

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

INN_RE = re.compile(r"(?:ИНН|inn)[:\s]*(\d{10,12})", re.IGNORECASE)
MAX_PAGES_PER_KEYWORD = 25   # 25 × 40 = 1000 записей на слово
PAGE_SIZE = 40
MAX_RETRIES = 3
DELAY_MIN = 1.5
DELAY_MAX = 4.0


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer":         "https://fedresurs.ru/",
        "Origin":          "https://fedresurs.ru",
    }


def _params(endpoint: str, query: str, limit: int, offset: int) -> dict:
    if "efrs-messages" in endpoint:
        return {"query": query, "limit": limit, "offset": offset}
    elif "backend/search" in endpoint:
        return {"searchString": query, "limit": limit, "offset": offset}
    else:
        return {"query": query, "limit": limit, "offset": offset}


def _extract_inn(item: dict) -> str:
    for field in ("entityInn", "inn", "companyInn", "participantInn",
                  "debtorInn", "lessorInn", "lesseeInn"):
        val = str(item.get(field, "")).strip()
        if re.fullmatch(r"\d{10,12}", val):
            return val
    text = " ".join(str(item.get(k, "")) for k in (
        "messageText", "text", "title", "description",
        "entityName", "debtorName", "subject",
    ))
    m = INN_RE.search(text)
    return m.group(1) if m else ""


def _extract_name(item: dict) -> str:
    for field in ("entityName", "debtorName", "lesseeName", "companyName",
                  "name", "organizationName"):
        val = str(item.get(field, "")).strip()
        if val and val.lower() not in ("null", "none", ""):
            return val
    return ""


def _extract_date(item: dict) -> str:
    for field in ("publishedDate", "date", "messageDate", "createdDate",
                  "contractDate", "signDate"):
        val = str(item.get(field, "")).strip()
        if val and val.lower() not in ("null", "none", ""):
            return val[:10]  # берём только дату YYYY-MM-DD
    return ""


def _extract_text(item: dict) -> str:
    parts = []
    for field in ("messageText", "text", "title", "description",
                  "subject", "contractSubject", "propertyDescription"):
        v = str(item.get(field, "")).strip()
        if v:
            parts.append(v)
    return " | ".join(parts)[:2000]


def _extract_role(item: dict) -> str:
    """Определяем роль: лизингополучатель или лизингодатель."""
    text = _extract_text(item).lower()
    msg_type = str(item.get("messageType", item.get("type", ""))).lower()

    if any(w in text for w in ("лизингополучатель", "lessee", "арендатор")):
        return "лизингополучатель"
    if any(w in text for w in ("лизингодатель", "lessor", "арендодатель")):
        return "лизингодатель"
    return "неизвестно"


def _normalise(item: dict, keyword: str) -> dict | None:
    """Нормализует одну запись из API. Возвращает None если ИНН не найден."""
    inn  = _extract_inn(item)
    name = _extract_name(item)
    if not inn and not name:
        return None
    return {
        "inn":      inn,
        "name":     name,
        "role":     _extract_role(item),
        "date":     _extract_date(item),
        "keyword":  keyword,
        "msg_type": str(item.get("messageType", item.get("type", ""))),
        "text":     _extract_text(item),
        "raw_id":   str(item.get("id", item.get("messageId", ""))),
    }


async def _probe_endpoint(client: httpx.AsyncClient) -> str | None:
    """Находит рабочий эндпоинт (пробный запрос)."""
    for ep in ENDPOINTS:
        try:
            r = await client.get(
                ep,
                params=_params(ep, "antminer", 1, 0),
                headers=_headers(),
                timeout=20,
            )
            log.info(f"Probe {ep}: HTTP {r.status_code}")
            if r.status_code == 200:
                try:
                    r.json()
                    return ep
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Probe {ep}: {e}")
    return None


async def _fetch_page(
    client: httpx.AsyncClient,
    endpoint: str,
    keyword: str,
    offset: int,
) -> tuple[list[dict], int]:
    """Загружает одну страницу. Возвращает (items, total)."""
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                endpoint,
                params=_params(endpoint, keyword, PAGE_SIZE, offset),
                headers=_headers(),
                timeout=25,
            )
            if r.status_code == 429:
                wait = 30 + random.uniform(0, 15)
                log.warning(f"Rate-limit 429 — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                log.warning(f"HTTP {r.status_code} для '{keyword}' offset={offset}")
                return [], 0

            data  = r.json()
            items = (
                data.get("data") or data.get("items") or
                data.get("content") or data.get("messages") or
                (data if isinstance(data, list) else [])
            )
            total = int(
                data.get("total") or data.get("totalElements") or
                data.get("count") or len(items) or 0
            )
            return items, total

        except Exception as e:
            log.debug(f"Attempt {attempt} keyword='{keyword}' offset={offset}: {e}")
            await asyncio.sleep(2 ** attempt)

    return [], 0


async def search_keyword(
    client: httpx.AsyncClient,
    endpoint: str,
    keyword: str,
    max_results: int,
) -> list[dict]:
    """Пагинированный обход по одному ключевому слову."""
    records: list[dict] = []
    offset = 0

    log.info(f"Поиск: «{keyword}»")
    while offset < min(max_results, MAX_PAGES_PER_KEYWORD * PAGE_SIZE):
        items, total = await _fetch_page(client, endpoint, keyword, offset)
        if not items:
            break

        if offset == 0:
            log.info(f"  «{keyword}»: всего {total} записей на Федресурсе")

        for item in items:
            rec = _normalise(item, keyword)
            if rec:
                records.append(rec)

        offset += PAGE_SIZE
        if total and offset >= total:
            break

        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    log.info(f"  «{keyword}»: собрано {len(records)} записей")
    return records


def _deduplicate(records: list[dict]) -> list[dict]:
    """Дедупликация по (inn, raw_id). Если ИНН пустой — по (name, date)."""
    seen: set[str] = set()
    result: list[dict] = []
    for r in records:
        key = r["inn"] + r["raw_id"] if r["inn"] else r["name"] + r["date"]
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def _save_csv(records: list[dict], path: str) -> None:
    fields = ["inn", "name", "role", "date", "keyword", "msg_type", "text", "raw_id"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    log.info(f"CSV сохранён: {path} ({len(records)} строк)")


async def main(out_path: str, max_per_keyword: int) -> None:
    Path("output").mkdir(exist_ok=True)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        endpoint = await _probe_endpoint(client)
        if not endpoint:
            log.error("Федресурс недоступен. Проверь подключение.")
            sys.exit(1)

        log.info(f"Используем эндпоинт: {endpoint}")

        all_records: list[dict] = []
        for kw in SEARCH_KEYWORDS:
            records = await search_keyword(client, endpoint, kw, max_per_keyword)
            all_records.extend(records)
            await asyncio.sleep(random.uniform(3.0, 6.0))

    deduped = _deduplicate(all_records)
    log.info(f"Итого уникальных записей: {len(deduped)} (из {len(all_records)} до дедупликации)")

    # Отдельный файл только с лизингополучателями
    lessees = [r for r in deduped if r["role"] == "лизингополучатель"
               or r["role"] == "неизвестно"]

    _save_csv(deduped, out_path)

    # Краткая сводка по ключевым словам
    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)
    print("\n" + "=" * 60)
    print(f"  ГОТОВО — Федресурс лизинг оборудования")
    print(f"  Всего записей:          {len(deduped)}")
    print(f"  С ИНН:                  {sum(1 for r in deduped if r['inn'])}")
    print(f"  Лизингополучателей:     {sum(1 for r in deduped if r['role'] == 'лизингополучатель')}")
    print(f"  CSV: {out_path}")
    print("\n  По ключевым словам:")
    for kw, cnt in kw_stats.most_common():
        print(f"    {kw:<35} {cnt}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Федресурс — поиск лизинга оборудования")
    parser.add_argument(
        "--out", default=f"output/fedresurs_leasing_{date.today()}.csv",
        help="Путь к выходному CSV"
    )
    parser.add_argument(
        "--limit", type=int, default=1000,
        help="Макс. записей на одно ключевое слово (default: 1000)"
    )
    args = parser.parse_args()

    asyncio.run(main(args.out, args.limit))
