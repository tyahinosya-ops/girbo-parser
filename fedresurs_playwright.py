"""
fedresurs_playwright.py  v5

Playwright-based scraper для Федресурса.
Ищет sfacts-сообщения о лизинге майнинг-оборудования через реальный браузер.

Стратегия:
  1. Открываем https://fedresurs.ru/
  2. Переходим к поиску сообщений (/search/messages или /sfacts)
  3. Для каждого ключевого слова — ищем, собираем результаты постранично
  4. Из каждого найденного сообщения извлекаем ИНН/название участников

Запуск:
    playwright install chromium --with-deps
    python fedresurs_playwright.py
    python fedresurs_playwright.py --out results.csv --headless
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
from urllib.parse import urlencode, urlparse, parse_qs

from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PWTimeout

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

PAGE_SIZE  = 20   # результатов на странице (сайт)
MAX_PAGES  = 100  # максимум страниц на ключевое слово

# ── Вспомогательные функции ─────────────────────────────────────────────────

def _extract_inn(text: str) -> str:
    m = re.search(r"\b(\d{10}|\d{12})\b", text or "")
    return m.group(1) if m else ""


async def _rand_delay(lo=0.8, hi=2.2):
    await asyncio.sleep(random.uniform(lo, hi))


# ── Парсинг страниц ─────────────────────────────────────────────────────────

async def _intercept_api_response(page: Page, api_data: list[dict], keyword: str) -> None:
    """Перехватываем JSON-ответы API, которые браузер получает при поиске."""
    pass  # заполним через route


async def search_keyword_playwright(page: Page, keyword: str) -> list[dict]:
    """Ищет ключевое слово через интерфейс Федресурса и собирает результаты."""
    results: list[dict] = []
    intercepted: list[dict] = []

    # Перехватываем все XHR/fetch запросы — ищем реальный API
    _API_HINTS = ("/backend/", "/api/", "/sfacts", "/search", "/messages")

    async def handle_response(response):
        if response.status != 200:
            return
        url = response.url
        if "fedresurs.ru" not in url:
            return
        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await response.json()
            if isinstance(body, (list, dict)):
                intercepted.append({"url": url, "body": body})
                log.info(f"  [API перехват] {url}")
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        # Открываем поиск сообщений
        search_url = f"{BASE}/search/messages?searchString={keyword}&limit={PAGE_SIZE}"
        log.info(f"  [{keyword}] → {search_url}")
        await page.goto(search_url, wait_until="networkidle", timeout=30_000)
        await _rand_delay(2, 4)

        for page_num in range(MAX_PAGES):
            # Ждём появления результатов или «ничего не найдено»
            try:
                await page.wait_for_selector(
                    ".search-result-item, .no-results, [class*='result'], "
                    "[class*='sfact'], table tbody tr, .message-item",
                    timeout=15_000,
                )
            except PWTimeout:
                log.warning(f"  [{keyword}] стр.{page_num}: таймаут ожидания результатов")
                break

            # Извлекаем сообщения из DOM
            items = await _extract_items_from_dom(page, keyword)
            if items:
                results.extend(items)
                log.info(f"  [{keyword}] стр.{page_num}: +{len(items)} (DOM)")

            # Также смотрим на перехваченные API-ответы
            for captured in intercepted:
                parsed = _parse_api_response(captured["body"], keyword)
                if parsed:
                    results.extend(parsed)
                    log.info(f"  [{keyword}] API {captured['url'][-60:]}: +{len(parsed)}")
            intercepted.clear()

            # Следующая страница — ищем кнопку «Следующая»
            has_next = await _click_next_page(page)
            if not has_next:
                break

            await _rand_delay(2, 4)

    finally:
        page.remove_listener("response", handle_response)

    return results


async def _extract_items_from_dom(page: Page, keyword: str) -> list[dict]:
    """Извлекает данные из DOM страницы результатов."""
    results = []
    try:
        # Находим все ссылки на сообщения sfacts
        links = await page.eval_on_selector_all(
            "a[href*='/sfacts/'], a[href*='/messages/'], a[href*='/notice/']",
            """links => links.map(a => ({
                href: a.href,
                text: a.innerText.trim().slice(0, 300)
            }))"""
        )
        for link in links:
            if not link["href"]:
                continue
            # Извлекаем ID из URL
            msg_id_m = re.search(r"/(sfacts|messages|notice)/([a-zA-Z0-9-]+)", link["href"])
            msg_id = msg_id_m.group(2) if msg_id_m else ""
            results.append({
                "keyword":     keyword,
                "lessee_inn":  _extract_inn(link["text"]),
                "lessee_name": "",
                "lessor_inn":  "",
                "lessor_name": "",
                "date":        "",
                "subject":     link["text"][:500],
                "msg_id":      msg_id,
                "msg_url":     link["href"],
            })
    except Exception as e:
        log.debug(f"DOM extract error: {e}")
    return results


async def _click_next_page(page: Page) -> bool:
    """Кликает кнопку следующей страницы. Возвращает True если успешно."""
    selectors = [
        "button:has-text('Следующая')",
        "a:has-text('Следующая')",
        "[aria-label='Next page']",
        ".pagination-next:not([disabled])",
        "button.next:not([disabled])",
        "li.next:not(.disabled) a",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_enabled():
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=10_000)
                return True
        except Exception:
            continue
    return False


def _parse_api_response(body: dict | list, keyword: str) -> list[dict]:
    """Парсит перехваченный JSON-ответ API."""
    results = []
    items: list[dict] = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = (
            body.get("items")
            or body.get("data")
            or body.get("messages")
            or body.get("sfacts")
            or []
        )
    for msg in items:
        if not isinstance(msg, dict):
            continue
        # Предмет
        subject = ""
        for k in ("subject", "contractSubject", "leaseSubject", "propertyDescription",
                  "description", "title", "text", "messageText"):
            if msg.get(k):
                subject = str(msg[k])[:1000]
                break
        # Лизингополучатель
        lessee_inn = lessee_name = ""
        for k in ("lessee", "pledgor", "debtor", "client"):
            p = msg.get(k)
            if isinstance(p, dict):
                lessee_inn  = str(p.get("inn") or p.get("code") or "")
                lessee_name = str(p.get("name") or p.get("fullName") or "")
                if lessee_inn or lessee_name:
                    break
        # Лизингодатель
        lessor_inn = lessor_name = ""
        for k in ("lessor", "pledgee", "creditor", "lender"):
            p = msg.get(k)
            if isinstance(p, dict):
                lessor_inn  = str(p.get("inn") or p.get("code") or "")
                lessor_name = str(p.get("name") or p.get("fullName") or "")
                if lessor_inn or lessor_name:
                    break
        msg_id = str(msg.get("id") or msg.get("guid") or "")
        pub_date = str(msg.get("publishedDate") or msg.get("date") or "")[:10]
        results.append({
            "keyword":     keyword,
            "lessee_inn":  lessee_inn,
            "lessee_name": lessee_name,
            "lessor_inn":  lessor_inn,
            "lessor_name": lessor_name,
            "date":        pub_date,
            "subject":     subject,
            "msg_id":      msg_id,
            "msg_url":     f"{BASE}/sfacts/{msg_id}" if msg_id else "",
        })
    return results


# ── Детальное открытие сообщения ─────────────────────────────────────────────

async def _enrich_record(page: Page, rec: dict) -> dict:
    """Открывает страницу сообщения и дополняет запись данными."""
    if not rec.get("msg_url") or rec.get("lessee_inn"):
        return rec
    try:
        await page.goto(rec["msg_url"], wait_until="domcontentloaded", timeout=20_000)
        await _rand_delay(0.5, 1.5)
        text = await page.inner_text("body")
        # ИНН из текста
        inns = re.findall(r"\b(\d{10}|\d{12})\b", text)
        if inns:
            rec["lessee_inn"] = inns[0]
        # Предмет — ищем в тексте строки с ключевым словом
        for line in text.splitlines():
            lo = line.lower()
            if any(kw in lo for kw in ("предмет", "объект", "имущество", "оборудование")):
                rec["subject"] = line.strip()[:500]
                break
    except Exception as e:
        log.debug(f"enrich {rec['msg_url']}: {e}")
    return rec


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(out_path: str, headless: bool = True, enrich: bool = False) -> None:
    all_records: list[dict] = []
    fields = [
        "keyword", "lessee_inn", "lessee_name",
        "lessor_inn", "lessor_name",
        "date", "subject", "msg_id", "msg_url",
    ]

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Принимаем cookie-баннер при первом открытии
        log.info("Открываем Федресурс ...")
        try:
            await page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
            await _rand_delay(1, 2)
            for btn_text in ("Принять", "Принять все", "Согласен", "OK"):
                btn = page.locator(f"button:has-text('{btn_text}')").first
                if await btn.count() > 0:
                    await btn.click()
                    await _rand_delay(0.5, 1)
                    break
        except Exception as e:
            log.warning(f"Открытие главной: {e}")

        for kw in KEYWORDS:
            log.info(f"Ключевое слово: «{kw}»")
            records = await search_keyword_playwright(page, kw)
            all_records.extend(records)
            await _rand_delay(3, 6)

        # Дополнительное обогащение (опционально, медленно)
        if enrich:
            log.info("Обогащаем записи (открываем каждое сообщение) ...")
            for i, rec in enumerate(all_records):
                all_records[i] = await _enrich_record(page, rec)
                if i % 10 == 0:
                    await _rand_delay(1, 2)

        await browser.close()

    # Дедупликация
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        key = r["msg_id"] or (r["lessee_inn"] + r["date"] + r["keyword"] + r["subject"][:30])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # Сохраняем CSV
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(deduped)

    log.info(f"CSV: {out_path} ({len(deduped)} строк)")

    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)
    print("\n" + "=" * 60)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ  v5 (Playwright)")
    print(f"  Найдено записей:           {len(deduped)}")
    print(f"  С ИНН лизингополучателя:  {sum(1 for r in deduped if r['lessee_inn'])}")
    if kw_stats:
        print("\n  По ключевым словам:")
        for kw, cnt in kw_stats.most_common():
            print(f"    {kw:<45} {cnt}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f"output/fedresurs_leasing_{date.today()}.csv")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--enrich", action="store_true",
                        help="Открывать каждое сообщение для дополнения данных")
    args = parser.parse_args()
    asyncio.run(main(args.out, headless=args.headless, enrich=args.enrich))
