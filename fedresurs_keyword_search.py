"""
fedresurs_keyword_search.py  v4

Стратегия — прямой full-text поиск по sfacts:
  GET /backend/sfacts?limit=N&offset=N&searchString=KEYWORD
  → возвращает все сообщения, где текст предмета договора содержит ключевое слово

Для каждого совпадения извлекаем:
  - ИНН и название лизингополучателя
  - ИНН и название лизингодателя
  - Предмет договора, дата, ссылка

Запуск:
    python fedresurs_keyword_search.py
    python fedresurs_keyword_search.py --out results.csv
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

# ── Ключевые слова для поиска в предмете лизинга ────────────────────────────
KEYWORDS: list[str] = [
    "antminer",
    "whatsminer",
    "bitmain",
    "microbt",
    "innosilicon",
    "asic майнер",
    "asic-майнер",
    "асик майнер",
    "bitcoin miner",
    "майнинговое оборудование",
    "оборудование для майнинга",
    "криптомайнер",
]

BASE_URL  = "https://fedresurs.ru"
PAGE_SIZE = 40
MAX_PAGES = 50   # максимум страниц на ключевое слово (~2000 записей)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Referer":         BASE_URL + "/",
        "Origin":          BASE_URL,
    }


async def _get(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> dict | list | None:
    url = BASE_URL + path
    for attempt in range(3):
        try:
            r = await client.get(url, params=params, headers=_headers(), timeout=25)
            if r.status_code == 429:
                wait = 30 + random.uniform(0, 10)
                log.warning(f"429 — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            log.debug(f"GET {path}: HTTP {r.status_code}")
            return None
        except Exception as e:
            log.debug(f"GET {path} attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return None


def _extract_inn(text: str) -> str:
    m = re.search(r"\b(\d{10}|\d{12})\b", text)
    return m.group(1) if m else ""


def _pick(obj: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = obj.get(k)
        if v and str(v).strip().lower() not in ("null", "none", ""):
            return str(v).strip()
    return default


def _parse_message(msg: dict, keyword: str) -> dict | None:
    """Разбирает одно sfacts-сообщение в строку результата."""
    # Предмет лизинга может быть в разных полях
    subject = _pick(
        msg,
        "subject", "contractSubject", "leaseSubject",
        "propertyDescription", "objectDescription",
        "description", "title", "text", "messageText",
    )

    # Лизингополучатель
    lessee_inn  = ""
    lessee_name = ""
    for key in ("lessee", "pledgor", "debtor", "client", "borrower"):
        party = msg.get(key)
        if isinstance(party, dict):
            lessee_inn  = _pick(party, "inn", "code", "tin")
            lessee_name = _pick(party, "name", "fullName", "shortName")
            if lessee_inn or lessee_name:
                break

    # Лизингодатель
    lessor_inn  = ""
    lessor_name = ""
    for key in ("lessor", "pledgee", "creditor", "lender", "company"):
        party = msg.get(key)
        if isinstance(party, dict):
            lessor_inn  = _pick(party, "inn", "code", "tin")
            lessor_name = _pick(party, "name", "fullName", "shortName")
            if lessor_inn or lessor_name:
                break

    # Fallback — ИНН из текста
    if not lessee_inn:
        lessee_inn = _extract_inn(subject)

    pub_date = _pick(msg, "publishedDate", "date", "messageDate", "datePublished", "createdDate")[:10]
    msg_id   = _pick(msg, "id", "guid", "messageId")
    msg_url  = f"{BASE_URL}/sfacts/{msg_id}" if msg_id else ""

    return {
        "keyword":      keyword,
        "lessee_inn":   lessee_inn,
        "lessee_name":  lessee_name,
        "lessor_inn":   lessor_inn,
        "lessor_name":  lessor_name,
        "date":         pub_date,
        "subject":      subject[:1000],
        "msg_id":       msg_id,
        "msg_url":      msg_url,
    }


async def search_keyword(
    client: httpx.AsyncClient, keyword: str
) -> list[dict]:
    """Ищет все sfacts-сообщения по ключевому слову."""
    results: list[dict] = []
    offset = 0

    for page in range(MAX_PAGES):
        params = {
            "limit":        PAGE_SIZE,
            "offset":       offset,
            "searchString": keyword,
        }
        data = await _get(client, "/backend/sfacts", params)

        if data is None:
            log.warning(f"  «{keyword}» стр.{page}: нет ответа")
            break

        # Нормализуем ответ
        if isinstance(data, list):
            items = data
            total = len(data)
        elif isinstance(data, dict):
            items = data.get("items") or data.get("data") or data.get("messages") or []
            total = int(data.get("total") or data.get("totalCount") or len(items) or 0)
        else:
            break

        if page == 0:
            log.info(f"  «{keyword}»: найдено ~{total} сообщений")

        if not items:
            break

        for msg in items:
            parsed = _parse_message(msg, keyword)
            if parsed:
                results.append(parsed)
                log.info(
                    f"    [{parsed['lessee_inn'] or '?':12}] "
                    f"{parsed['lessee_name'] or '(без имени)':<35} | "
                    f"{parsed['subject'][:60]}"
                )

        offset += PAGE_SIZE
        if total and offset >= total:
            break

        await asyncio.sleep(random.uniform(1.5, 3.0))

    log.info(f"  «{keyword}»: итого {len(results)} записей")
    return results


async def main(out_path: str) -> None:
    all_records: list[dict] = []
    api_reachable = False

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=3),
    ) as client:
        # Проверка доступности эндпоинта
        log.info("Проверяем /backend/sfacts ...")
        probe = await _get(client, "/backend/sfacts", {"limit": 1, "offset": 0, "searchString": "лизинг"})
        if probe is not None:
            api_reachable = True
            log.info("API доступен — начинаем поиск")
        else:
            log.error(
                "GET /backend/sfacts недоступен с этого IP.\n"
                "Попробуйте запустить локально или через VPN.\n"
                "Сохраняем пустой CSV и выходим."
            )

        if api_reachable:
            for kw in KEYWORDS:
                log.info(f"Поиск: «{kw}»")
                records = await search_keyword(client, kw)
                all_records.extend(records)
                await asyncio.sleep(random.uniform(3.0, 6.0))

    # Дедупликация по ID сообщения
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        key = r["msg_id"] or (r["lessee_inn"] + r["date"] + r["keyword"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # Сохраняем CSV
    fields = [
        "keyword", "lessee_inn", "lessee_name",
        "lessor_inn", "lessor_name",
        "date", "subject", "msg_id", "msg_url",
    ]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(deduped)
    log.info(f"CSV: {out_path} ({len(deduped)} строк)")

    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)

    print("\n" + "=" * 60)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ  v4")
    print(f"  API доступен:              {'да' if api_reachable else 'НЕТ'}")
    print(f"  Найдено записей:           {len(deduped)}")
    print(f"  С ИНН лизингополучателя:  {sum(1 for r in deduped if r['lessee_inn'])}")
    if kw_stats:
        print("\n  По ключевым словам:")
        for kw, cnt in kw_stats.most_common():
            print(f"    {kw:<45} {cnt}")
    print("=" * 60 + "\n")

    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f"output/fedresurs_leasing_{date.today()}.csv")
    args = parser.parse_args()
    asyncio.run(main(args.out))
