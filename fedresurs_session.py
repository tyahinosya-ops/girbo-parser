"""
fedresurs_session.py  v7

Цель: выгрузить ВСЕ записи Федресурса о лизинге майнинг-оборудования.

Стратегия ключевых слов:
  EXACT  — бренды/модели, однозначно указывают на майнинг (фильтрация не нужна)
  BROAD  — общие термины (ЭВМ, сервер), принимаются только если в тексте
           записи есть хотя бы одно слово из MINING_INDICATORS

Запуск:
    python fedresurs_session.py
    python fedresurs_session.py --out results.csv
    PROXY_URL=socks5://user:pass@host:1080 python fedresurs_session.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
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

# ---------------------------------------------------------------------------
# Ключевые слова
# ---------------------------------------------------------------------------

# EXACT — достаточно найти в тексте, однозначно майнинг
KEYWORDS_EXACT: list[str] = [
    # Bitmain
    "antminer",
    "bitmain",
    # MicroBT
    "whatsminer",
    "microbt",
    # Canaan Creative
    "canaan",
    "avalon miner",
    # Innosilicon
    "innosilicon",
    # Goldshell
    "goldshell",
    # Jasminer (Ethash)
    "jasminer",
    # Ebang
    "ebang",
    # iBeLink
    "ibelink",
    # Общие термины, достаточно специфичные
    "асик",                           # кириллица
    "asic",                           # латиница
    "майнинговое оборудование",
    "оборудование для майнинга",
    "оборудование для добычи криптовалют",
    "добыча криптовалюты",
    "майнинг криптовалют",
    "хэшрейт",
    "hashrate",
]

# BROAD — требуют подтверждения через MINING_INDICATORS
KEYWORDS_BROAD: list[str] = [
    "эвм",                            # электронная вычислительная машина
    "вычислительное оборудование",
    "серверное оборудование",
]

# Слова, подтверждающие майнинг-контекст для BROAD-запросов
MINING_INDICATORS: frozenset[str] = frozenset({
    "antminer", "whatsminer", "bitmain", "microbt", "canaan",
    "innosilicon", "goldshell", "jasminer", "ebang", "ibelink",
    "асик", "asic", "майнинг", "mining", "криптовалют",
    "хэшрейт", "hashrate", "blockchain", "блокчейн",
    "добыча цифровой", "цифровая валюта",
})

PAGE_SIZE = 40
MAX_PAGES = 100  # ~4000 записей на ключевое слово

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Варианты search-эндпоинтов — перебираем пока не найдём рабочий
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


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

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
    """Открывает главную страницу — получаем куки и XSRF-токен."""
    try:
        r = await client.get(BASE + "/", timeout=20, headers={"User-Agent": UA})
        log.info(f"Главная: HTTP {r.status_code}, куки: {list(client.cookies.keys())}")
        return client.cookies.get("XSRF-TOKEN", "")
    except Exception as e:
        log.warning(f"Ошибка init_session: {e}")
        return ""


def _detect_items(data: dict | list) -> tuple[list, int]:
    """Извлекает items и total из ответа с любой структурой."""
    if isinstance(data, list):
        return data, len(data)
    if not isinstance(data, dict):
        return [], 0
    for val in data.values():
        if isinstance(val, list) and len(val) > 0:
            total = 0
            for tkey in ("total", "totalCount", "totalElements", "count", "size"):
                if data.get(tkey):
                    total = int(data[tkey])
                    break
            return val, total or len(val)
    for tkey in ("total", "totalCount", "totalElements", "count", "size"):
        if data.get(tkey) and int(data[tkey]) > 0:
            return [], int(data[tkey])
    return [], 0


async def _probe_endpoint(
    client: httpx.AsyncClient, method: str, path: str, param: str, xsrf: str
) -> bool:
    """Пробует эндпоинт с probe-словом «лизинг». True — данные есть."""
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
        log.info(f"  RAW: {raw}")
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
    """Запрашивает одну страницу. Возвращает (items, total)."""
    url = BASE + path
    for attempt in range(3):
        try:
            if method == "GET":
                r = await client.get(
                    url,
                    params={"limit": PAGE_SIZE, "offset": offset, param: keyword},
                    headers=_base_headers(xsrf),
                    timeout=25,
                )
            else:
                r = await client.post(
                    url,
                    json={"limit": PAGE_SIZE, "offset": offset, param: keyword},
                    headers={**_base_headers(xsrf), "Content-Type": "application/json"},
                    timeout=25,
                )
            if r.status_code == 429:
                wait = 30 + random.uniform(0, 15)
                log.warning(f"429 — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                return [], 0
            return _detect_items(r.json())
        except Exception as e:
            log.debug(f"fetch_page attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)
    return [], 0


# ---------------------------------------------------------------------------
# Парсинг одного сообщения
# ---------------------------------------------------------------------------

def _get_nested(d: dict, *keys: str) -> str:
    """Безопасно достаёт первое непустое значение из словаря d по списку ключей."""
    for k in keys:
        v = d.get(k)
        if v and str(v).strip().lower() not in ("null", "none", ""):
            return str(v).strip()
    return ""


def _parse_msg(msg: dict, keyword: str) -> dict:
    # Предмет лизинга / описание объекта
    subject = _get_nested(
        msg,
        "subject", "contractSubject", "leaseSubject",
        "propertyDescription", "objectDescription",
        "description", "title", "text", "messageText",
    )

    # Лизингополучатель
    lessee_inn = lessee_name = ""
    for role_key in ("lessee", "pledgor", "debtor", "client", "borrower"):
        party = msg.get(role_key)
        if isinstance(party, dict):
            lessee_inn  = _get_nested(party, "inn", "code", "tin", "ogrn")
            lessee_name = _get_nested(party, "name", "fullName", "shortName", "title")
            if lessee_inn or lessee_name:
                break

    # Лизингодатель
    lessor_inn = lessor_name = ""
    for role_key in ("lessor", "pledgee", "creditor", "lender", "financier"):
        party = msg.get(role_key)
        if isinstance(party, dict):
            lessor_inn  = _get_nested(party, "inn", "code", "tin", "ogrn")
            lessor_name = _get_nested(party, "name", "fullName", "shortName", "title")
            if lessor_inn or lessor_name:
                break

    # Fallback: ИНН из текста предмета
    if not lessee_inn:
        lessee_inn = _extract_inn(subject)

    msg_id   = str(msg.get("id") or msg.get("guid") or msg.get("messageId") or "")
    pub_date = str(msg.get("publishedDate") or msg.get("date") or msg.get("messageDate") or "")[:10]

    return {
        "keyword":     keyword,
        "lessee_inn":  lessee_inn,
        "lessee_name": lessee_name[:200],
        "lessor_inn":  lessor_inn,
        "lessor_name": lessor_name[:200],
        "date":        pub_date,
        "subject":     subject[:1000],
        "msg_id":      msg_id,
        "msg_url":     f"{BASE}/sfacts/{msg_id}" if msg_id else "",
    }


def _msg_text(msg: dict) -> str:
    """Всё текстовое содержимое сообщения в нижнем регистре."""
    return " ".join(
        str(v) for v in msg.values()
        if isinstance(v, (str, int, float))
    ).lower()


def _is_relevant(msg: dict, keyword: str, exact: bool) -> bool:
    """
    Проверяет релевантность сообщения.
    exact=True  → достаточно наличия keyword в тексте
    exact=False → keyword + хотя бы один MINING_INDICATOR
    """
    text = _msg_text(msg)
    if keyword.lower() not in text:
        return False
    if exact:
        return True
    return any(ind in text for ind in MINING_INDICATORS)


# ---------------------------------------------------------------------------
# Поиск по одному ключевому слову
# ---------------------------------------------------------------------------

async def search_keyword(
    client: httpx.AsyncClient,
    method: str, path: str, param: str, xsrf: str,
    keyword: str,
    exact: bool,
) -> list[dict]:
    results: list[dict] = []
    offset = 0
    for page_num in range(MAX_PAGES):
        items, total = await _fetch_page(
            client, method, path, param, xsrf, keyword, offset
        )
        if not items:
            break
        if page_num == 0:
            log.info(f"  «{keyword}»: всего~{total} записей в API")

        for m in items:
            if _is_relevant(m, keyword, exact):
                rec = _parse_msg(m, keyword)
                results.append(rec)
                log.info(
                    f"    [{rec['lessee_inn'] or '?':12}] "
                    f"{rec['lessee_name'] or '(без имени)':<35} | "
                    f"{rec['subject'][:60]}"
                )

        offset += PAGE_SIZE
        if total and offset >= total:
            break
        await asyncio.sleep(random.uniform(1.0, 2.5))

    log.info(f"  «{keyword}»: отобрано {len(results)} записей")
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main(out_path: str) -> None:
    fields = [
        "keyword", "lessee_inn", "lessee_name",
        "lessor_inn", "lessor_name",
        "date", "subject", "msg_id", "msg_url",
    ]

    # Прокси — socks5://user:pass@host:port  или  http://host:port
    proxy_url = (
        os.environ.get("PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
    )
    if proxy_url:
        safe = proxy_url.split("@")[-1]  # скрываем логин/пароль в логах
        log.info(f"Прокси: {safe}")
    else:
        log.warning(
            "PROXY_URL не задан. fedresurs.ru защищён Qrator "
            "и блокирует IP GitHub Actions (ConnectTimeout).\n"
            "Задайте секрет PROXY_URL=socks5://user:pass@host:port"
        )

    client_kwargs: dict = dict(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=3),
    )
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    all_records: list[dict] = []

    async with httpx.AsyncClient(**client_kwargs) as client:
        xsrf = await _init_session(client)

        # Находим рабочий эндпоинт
        working: tuple | None = None
        log.info("Ищем рабочий search-эндпоинт ...")
        for ep_method, ep_path, ep_param in SEARCH_ENDPOINTS:
            if await _probe_endpoint(client, ep_method, ep_path, ep_param, xsrf):
                working = (ep_method, ep_path, ep_param)
                log.info(f"✓ Рабочий эндпоинт: {ep_method} {ep_path} ?{ep_param}=...")
                break
            await asyncio.sleep(1.0)

        if working is None:
            log.warning(
                "Ни один search-эндпоинт не ответил данными.\n"
                "Причины: блокировка IP / смена API / таймаут.\n"
                "Смотрите строки RAW выше."
            )
        else:
            ep_method, ep_path, ep_param = working

            # Сначала точные (exact), потом широкие (broad) с доп-фильтром
            todo = (
                [(kw, True)  for kw in KEYWORDS_EXACT] +
                [(kw, False) for kw in KEYWORDS_BROAD]
            )
            for kw, exact in todo:
                kind = "exact" if exact else "broad+filter"
                log.info(f"Ключевое слово [{kind}]: «{kw}»")
                recs = await search_keyword(
                    client, ep_method, ep_path, ep_param, xsrf, kw, exact
                )
                all_records.extend(recs)
                await asyncio.sleep(random.uniform(2.0, 5.0))

    # Дедупликация по msg_id или составному ключу
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        key = r["msg_id"] or (
            r["lessee_inn"] + "|" + r["date"] + "|" + r["subject"][:40]
        )
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

    # Итоговая статистика
    from collections import Counter
    kw_stats = Counter(r["keyword"] for r in deduped)
    inn_count = sum(1 for r in deduped if r["lessee_inn"])

    print("\n" + "=" * 65)
    print("  ФЕДРЕСУРС — ЛИЗИНГ МАЙНИНГ-ОБОРУДОВАНИЯ  v7")
    print(f"  Рабочий эндпоинт:    {working or 'НЕ НАЙДЕН'}")
    print(f"  Найдено записей:     {len(deduped)}")
    print(f"  С ИНН лизингополуч.: {inn_count}")
    if kw_stats:
        print("\n  По ключевым словам:")
        for kw, cnt in kw_stats.most_common():
            print(f"    {kw:<50} {cnt}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=f"output/fedresurs_leasing_{date.today()}.csv",
    )
    args = parser.parse_args()
    asyncio.run(main(args.out))
