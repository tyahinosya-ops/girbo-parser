"""
fedresurs_keyword_search.py  v3

Стратегия (лизингодатель-first):
  1. Берём ИНН ~25 крупнейших российских лизинговых компаний
  2. GET /backend/companies?code=INN — получаем GUID каждого лизингодателя
  3. GET /backend/companies/{guid}/publications — все sfacts-публикации
  4. Фильтруем публикации, где предмет аренды содержит слова об оборудовании:
     antminer, whatsminer, bitmain, asic, майнер, s19, m30 и др.
  5. Из отфильтрованных сообщений извлекаем ИНН лизингополучателя

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

# ── Крупнейшие российские лизинговые компании (ИНН) ─────────────────────────
LESSORS: dict[str, str] = {
    "7709378229": "ВТБ Лизинг",
    "7707009586": "Сбербанк Лизинг",
    "7722208925": "Газпромбанк Лизинг",
    "7728168971": "Альфа-Лизинг",
    "7709429907": "Элемент Лизинг",
    "9705101614": "ЛК Европлан",
    "7709431192": "РЕСО-Лизинг",
    "7826696521": "Балтийский Лизинг",
    "7709878524": "МКБ Лизинг",
    "7709868284": "Совкомбанк Лизинг",
    "7841337843": "Интерлизинг",
    "7720261827": "ГТЛК",
    "7707368647": "ПСБ Лизинг",
    "7703425988": "Росбанк Лизинг",
    "7731279540": "Контрол Лизинг",
    "7714024816": "Стоун-XXI",
    "3905011110": "Каркаде",
    "7728543950": "Райффайзен-Лизинг",
    "7709863115": "УралСиб Лизинг",
    "7710297696": "Сименс Финанс",
    "7704329733": "Роделен Лизинг",
    "7805633430": "Петербургская Лизинговая Компания",
    "7725527688": "Лизинговая Компания МСП",
    "7736050003": "Росагролизинг",
    "5027219985": "ПР-Лизинг",
}

# ── Ключевые слова для фильтрации предмета лизинга ──────────────────────────
EQUIPMENT_KW: list[str] = [
    "antminer", "whatsminer", "bitmain", "microbt", "innosilicon",
    "asic", "asik", "асик",
    "майнер", "miner", "майнинг", "mining",
    "s19", "s21", "s17", "t19", "t21",
    "m30", "m50", "m60", "m20",
    "криптовалют", "bitcoin miner", "btc miner",
    "вычислительное оборудование для добыч",  # официальная формулировка
]

BASE_URL    = "https://fedresurs.ru"
PAGE_SIZE   = 100
MAX_RETRIES = 3

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


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | list | None:
    url = BASE_URL + path
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url, params=params, headers=_headers(), timeout=25)
            if r.status_code == 429:
                await asyncio.sleep(30 + random.uniform(0, 10))
                continue
            if r.status_code == 200:
                return r.json()
            log.debug(f"GET {path}: HTTP {r.status_code}")
            return None
        except Exception as e:
            log.debug(f"GET {path} attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return None


async def _find_company_guid(client: httpx.AsyncClient, inn: str) -> str | None:
    """Ищет GUID компании по ИНН через GET /backend/companies?code=INN."""
    data = await _get(client, "/backend/companies", {"limit": 5, "offset": 0, "code": inn})
    if not data:
        return None
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    for item in items:
        guid = str(item.get("guid") or item.get("id") or "").strip()
        if guid:
            return guid
    return None


async def _get_publications(
    client: httpx.AsyncClient, guid: str, start: int = 0
) -> tuple[list[dict], int]:
    """GET /backend/companies/{guid}/publications с фильтром sfacts."""
    params = {
        "limit": PAGE_SIZE,
        "offset": start,
        "searchSfactsMessage": "true",
        "searchFirmBankruptMessage": "false",
        "searchAmReport": "false",
        "searchCompanyEfrsb": "false",
        "searchSroAmMessage": "false",
        "searchTradeOrgMessage": "false",
    }
    data = await _get(client, f"/backend/companies/{guid}/publications", params)
    if not data:
        return [], 0
    if isinstance(data, list):
        return data, len(data)
    items = data.get("items") or data.get("data") or data.get("publications") or []
    total = int(data.get("total") or data.get("totalCount") or len(items) or 0)
    return items, total


def _pub_text(pub: dict) -> str:
    """Собирает весь текст публикации."""
    parts = []
    for key in (
        "title", "text", "messageText", "description",
        "subject", "contractSubject", "propertyDescription",
        "content", "leaseSubject", "objectDescription",
    ):
        v = str(pub.get(key, "")).strip()
        if v and v.lower() not in ("null", "none"):
            parts.append(v)
    return " | ".join(parts)[:4000]


def _has_equipment(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in EQUIPMENT_KW)


def _extract_inn(text: str) -> str:
    """Пытается найти ИНН (10 или 12 цифр) в тексте."""
    m = re.search(r"\b(\d{10}|\d{12})\b", text)
    return m.group(1) if m else ""


def _pub_date(pub: dict) -> str:
    for key in ("publishedDate", "date", "messageDate", "datePublished", "createdDate"):
        v = str(pub.get(key, "")).strip()
        if v and v.lower() not in ("null", "none"):
            return v[:10]
    return ""


async def run_search(client: httpx.AsyncClient) -> list[dict]:
    results: list[dict] = []
    api_ok = False

    for inn, lessor_name in LESSORS.items():
        log.info(f"Лизингодатель: {lessor_name} [{inn}]")

        guid = await _find_company_guid(client, inn)
        if not guid:
            log.warning(f"  GUID не найден для {lessor_name} — пропускаем")
            continue

        api_ok = True
        log.info(f"  GUID: {guid} — получаем публикации...")

        start = 0
        pub_count = 0
        match_count = 0

        while True:
            pubs, total = await _get_publications(client, guid, start)
            if not pubs:
                break

            if start == 0:
                log.info(f"  Всего публикаций: {total}")

            for pub in pubs:
                pub_count += 1
                text = _pub_text(pub)
                if not _has_equipment(text):
                    continue

                match_count += 1

                # Пытаемся достать ИНН лизингополучателя из вложенных данных
                lessee_inn  = ""
                lessee_name = ""
                for party_key in ("lessee", "pledgor", "debtor", "client", "company"):
                    party = pub.get(party_key) or {}
                    if isinstance(party, dict):
                        lessee_inn  = str(party.get("inn") or party.get("code") or "").strip()
                        lessee_name = str(party.get("name") or party.get("fullName") or "").strip()
                        if lessee_inn or lessee_name:
                            break

                # Fallback: ищем ИНН в тексте
                if not lessee_inn:
                    lessee_inn = _extract_inn(text)

                results.append({
                    "lessee_inn":   lessee_inn,
                    "lessee_name":  lessee_name,
                    "lessor_inn":   inn,
                    "lessor_name":  lessor_name,
                    "date":         _pub_date(pub),
                    "text":         text,
                    "pub_id":       str(pub.get("id") or pub.get("guid") or ""),
                    "pub_url":      f"{BASE_URL}/company/{guid}" if not pub.get("id") else "",
                })
                log.info(f"    ✓ [{lessee_inn}] {lessee_name or '?'} — {text[:80]}...")

            start += PAGE_SIZE
            if total and start >= total:
                break
            await asyncio.sleep(random.uniform(1.0, 2.5))

        log.info(f"  Проверено: {pub_count}, совпадений: {match_count}")
        await asyncio.sleep(random.uniform(2.0, 4.0))

    if not api_ok:
        log.error("Федресурс API недоступен с GitHub Actions — нет ни одного GUID")

    return results


def _deduplicate(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in records:
        key = r["pub_id"] or (r["lessee_inn"] + r["date"] + r["text"][:80])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _save_csv(records: list[dict], path: str) -> None:
    fields = ["lessee_inn", "lessee_name", "lessor_inn", "lessor_name",
              "date", "pub_id", "pub_url", "text"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    log.info(f"CSV сохранён: {path} ({len(records)} строк)")


async def main(out_path: str) -> None:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=5),
    ) as client:
        records = await run_search(client)

    deduped = _deduplicate(records)
    _save_csv(deduped, out_path)

    from collections import Counter
    lessor_stats = Counter(r["lessor_name"] for r in deduped)

    print("\n" + "=" * 60)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ  v3")
    print(f"  Лизингодателей проверено:  {len(LESSORS)}")
    print(f"  Найдено записей:           {len(deduped)}")
    print(f"  С ИНН лизингополучателя:  {sum(1 for r in deduped if r['lessee_inn'])}")
    if lessor_stats:
        print("\n  По лизингодателям:")
        for name, cnt in lessor_stats.most_common():
            print(f"    {name:<40} {cnt}")
    print("=" * 60 + "\n")

    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f"output/fedresurs_leasing_{date.today()}.csv")
    args = parser.parse_args()
    asyncio.run(main(args.out))
