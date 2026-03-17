"""
fedresurs_keyword_search.py  v2

Стратегия:
  1. POST /backend/companies/search  — ищем компании по ключевому слову в названии
  2. POST /backend/companies/publications — для каждой компании берём публикации
  3. Фильтруем публикации на слова: antminer, whatsminer, bitmain, microbt, эвм, сервер
  4. Playwright-fallback, если API закрыт по IP

Запуск:
    python fedresurs_keyword_search.py
    python fedresurs_keyword_search.py --out results.csv --limit 500
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

# ── Ключевые слова для поиска компаний и фильтрации текстов ───────────────
SEARCH_KEYWORDS: list[str] = [
    "antminer", "whatsminer", "bitmain", "microbt",
    "асик", "майнинг", "mining", "криптовалют",
    "эвм лизинг", "серверное оборудование лизинг",
]

# Ключевые слова для фильтрации текста публикаций
EQUIPMENT_KEYWORDS: list[str] = [
    "antminer", "whatsminer", "bitmain", "microbt",
    "асик", "asic", "майнер", "miner",
    "s19", "s21", "m30", "m50", "m60",   # модели оборудования
    "криптомайнер", "криптовалюта",
]

BASE_URL = "https://fedresurs.ru"

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

MAX_RETRIES  = 3
DELAY_MIN    = 1.5
DELAY_MAX    = 4.0
PAGE_SIZE    = 100


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Content-Type":    "application/json",
        "Origin":          BASE_URL,
        "Referer":         BASE_URL + "/",
    }


async def _post(
    client: httpx.AsyncClient, path: str, body: dict
) -> dict | list | None:
    url = BASE_URL + path
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.post(url, json=body, headers=_headers(), timeout=25)
            if r.status_code == 429:
                wait = 30 + random.uniform(0, 10)
                log.warning(f"429 rate-limit — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            log.debug(f"POST {path}: HTTP {r.status_code}")
            return None
        except Exception as e:
            log.debug(f"POST {path} attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return None


async def _search_companies(
    client: httpx.AsyncClient, keyword: str, start: int = 0
) -> tuple[list[dict], int]:
    """POST /backend/companies/search → (список компаний, всего)."""
    body = {
        "entitySearchFilter": {
            "onlyActive": False,
            "startRowIndex": start,
            "pageSize": PAGE_SIZE,
            "name": keyword,
        }
    }
    data = await _post(client, "/backend/companies/search", body)
    if not data:
        return [], 0
    if isinstance(data, dict):
        items = data.get("items") or data.get("data") or data.get("companies") or []
        total = int(data.get("total") or data.get("totalCount") or len(items) or 0)
        return items, total
    if isinstance(data, list):
        return data, len(data)
    return [], 0


async def _get_publications(
    client: httpx.AsyncClient, guid: str, start: int = 0
) -> tuple[list[dict], int]:
    """POST /backend/companies/publications → (публикации, всего)."""
    body = {
        "guid": guid,
        "pageSize": PAGE_SIZE,
        "startRowIndex": start,
        "searchSfactsMessage": True,
        "searchFirmBankruptMessage": False,
        "searchAmReport": False,
        "searchCompanyEfrsb": False,
    }
    data = await _post(client, "/backend/companies/publications", body)
    if not data:
        return [], 0
    if isinstance(data, dict):
        items = data.get("items") or data.get("data") or data.get("publications") or []
        total = int(data.get("total") or data.get("totalCount") or len(items) or 0)
        return items, total
    if isinstance(data, list):
        return data, len(data)
    return [], 0


def _pub_text(pub: dict) -> str:
    parts = []
    for key in ("title", "text", "messageText", "description", "subject",
                "contractSubject", "propertyDescription", "content"):
        v = str(pub.get(key, "")).strip()
        if v:
            parts.append(v)
    return " | ".join(parts)[:3000]


def _has_equipment(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in EQUIPMENT_KEYWORDS)


def _pub_date(pub: dict) -> str:
    for key in ("publishedDate", "date", "messageDate", "datePublished", "createdDate"):
        v = str(pub.get(key, "")).strip()
        if v and v.lower() not in ("null", "none", ""):
            return v[:10]
    return ""


def _pub_type(pub: dict) -> str:
    return str(pub.get("messageType") or pub.get("type") or "")


async def search_http(client: httpx.AsyncClient, max_per_kw: int) -> list[dict]:
    """Основной поиск через HTTP API."""
    results: list[dict] = []

    for kw in SEARCH_KEYWORDS:
        log.info(f"Поиск компаний: «{kw}»")
        start = 0
        companies_found = 0

        while True:
            companies, total = await _search_companies(client, kw, start)
            if not companies:
                break
            if start == 0:
                log.info(f"  «{kw}»: найдено {total} компаний")

            for company in companies:
                guid = str(company.get("guid") or company.get("id") or "").strip()
                name = str(company.get("name") or company.get("entityName") or "").strip()
                inn  = str(company.get("inn") or company.get("code") or "").strip()

                if not guid:
                    continue

                # Получаем публикации компании
                pub_start = 0
                while True:
                    pubs, pub_total = await _get_publications(client, guid, pub_start)
                    if not pubs:
                        break

                    for pub in pubs:
                        text = _pub_text(pub)
                        if _has_equipment(text):
                            results.append({
                                "inn":      inn,
                                "name":     name,
                                "role":     "лизингополучатель (предположительно)",
                                "date":     _pub_date(pub),
                                "keyword":  kw,
                                "msg_type": _pub_type(pub),
                                "text":     text,
                                "raw_id":   str(pub.get("id") or pub.get("guid") or ""),
                            })
                            log.info(f"    ✓ {name} [{inn}] — найдено оборудование")

                    pub_start += PAGE_SIZE
                    if pub_total and pub_start >= pub_total:
                        break
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                companies_found += 1
                await asyncio.sleep(random.uniform(DELAY_MIN / 2, DELAY_MAX / 2))

            start += PAGE_SIZE
            if total and start >= min(total, max_per_kw):
                break
            if not companies:
                break
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        log.info(f"  «{kw}»: обработано {companies_found} компаний")
        await asyncio.sleep(random.uniform(2.0, 5.0))

    return results


async def search_playwright(max_per_kw: int) -> list[dict]:
    """Playwright-fallback: браузерный поиск на fedresurs.ru."""
    results: list[dict] = []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("Playwright не установлен: pip install playwright && playwright install chromium")
        return results

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for kw in SEARCH_KEYWORDS:
            log.info(f"Playwright поиск: «{kw}»")
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="ru-RU",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf}", lambda r: r.abort())

            url = f"{BASE_URL}/search/entities?searchString={kw}"
            try:
                await page.goto(url, timeout=30_000, wait_until="networkidle")
                await asyncio.sleep(random.uniform(2.0, 4.0))

                full_text = await page.inner_text("body")
                lines = [l.strip() for l in full_text.splitlines() if l.strip()]

                # Ищем строки с ключевыми словами оборудования
                for i, line in enumerate(lines):
                    if _has_equipment(line):
                        context_lines = lines[max(0, i-5):i+10]
                        block = " | ".join(context_lines)
                        # Пытаемся вытащить ИНН из блока
                        inn_m = re.search(r"\b(\d{10}|\d{12})\b", block)
                        inn = inn_m.group(1) if inn_m else ""
                        results.append({
                            "inn":      inn,
                            "name":     "",
                            "role":     "неизвестно",
                            "date":     "",
                            "keyword":  kw,
                            "msg_type": "playwright",
                            "text":     block[:2000],
                            "raw_id":   "",
                        })

            except Exception as e:
                log.warning(f"Playwright «{kw}»: {e}")

            await context.close()
            await asyncio.sleep(random.uniform(3.0, 6.0))

        await browser.close()

    return results


def _deduplicate(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for r in records:
        key = (r["inn"] + r["raw_id"]) if (r["inn"] or r["raw_id"]) else r["text"][:100]
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def _save_csv(records: list[dict], path: str) -> None:
    fields = ["inn", "name", "role", "date", "keyword", "msg_type", "text", "raw_id"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    log.info(f"CSV: {path} ({len(records)} строк)")


async def main(out_path: str, max_per_kw: int) -> None:
    all_records: list[dict] = []
    api_ok = False

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        # Проверяем доступность API (тестовый запрос)
        log.info("Проверяем доступность Федресурс API...")
        test_companies, test_total = await _search_companies(client, "лизинг", 0)
        if test_companies or test_total > 0:
            log.info(f"API доступен (тест: {test_total} компаний)")
            api_ok = True
            all_records = await search_http(client, max_per_kw)
        else:
            log.warning("HTTP API недоступен — переключаемся на Playwright")

    if not api_ok:
        all_records = await search_playwright(max_per_kw)

    deduped = _deduplicate(all_records)
    _save_csv(deduped, out_path)

    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)

    print("\n" + "=" * 60)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ")
    print(f"  Найдено записей:    {len(deduped)}")
    print(f"  С ИНН:              {sum(1 for r in deduped if r['inn'])}")
    print(f"  Метод:              {'HTTP API' if api_ok else 'Playwright'}")
    print(f"  CSV:                {out_path}")
    if kw_stats:
        print("\n  По ключевым словам:")
        for kw, cnt in kw_stats.most_common():
            print(f"    {kw:<40} {cnt}")
    print("=" * 60 + "\n")

    # Всегда выходим с кодом 0 — артефакты должны сохраниться
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f"output/fedresurs_leasing_{date.today()}.csv")
    parser.add_argument("--limit", type=int, default=500,
                        help="Макс. компаний на ключевое слово")
    args = parser.parse_args()
    asyncio.run(main(args.out, args.limit))
