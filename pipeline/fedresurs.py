"""
fedresurs.py — поиск лизинговых договоров на Федресурсе

ПУТЬ А (майнеры)   — ищем по ключевым словам, берём ЛИЗИНГОПОЛУЧАТЕЛЕЙ
ПУТЬ Б (хостинги)  — те же договоры, берём ЛИЗИНГОДАТЕЛЕЙ (не финансовых)
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
    USER_AGENTS, PROXIES, MAX_RETRIES,
)

log = logging.getLogger(__name__)

INN_RE = re.compile(r"(?:ИНН|inn)[:\s]*(\d{10,12})", re.IGNORECASE)

FEDRESURS_ENDPOINTS = [
    "https://fedresurs.ru/backend/efrs-messages",
    "https://fedresurs.ru/backend/search",
    "https://fedresurs.ru/api/messages",
]

LEASING_MESSAGE_TYPES = [
    "ФинансоваяАренда",
    "Leasing",
    "LeasingContract",
    "УведомлениеОЛизинге",
]

# ── Ключевые слова для поиска майнингового оборудования ─────────────────────
MINING_SEARCH_KEYWORDS: list[str] = [
    "antminer",
    "whatsminer",
    "bitmain",
    "microbt",
    "асик",
    "asic",
    "майнер",
    "miner",
    "эвм",
    "криптовалют",
]

# ── Фильтрация финансовых лизингодателей ────────────────────────────────────
FINANCIAL_OKVED_PREFIXES: tuple[str, ...] = ("64.", "65.", "66.")
FINANCIAL_OKVED_EXACT: set[str] = {"64.91", "64.19", "64.99"}
FINANCIAL_NAME_KEYWORDS: tuple[str, ...] = (
    "банк", "страхов", "финансов", "кредит",
    "мфо", "микрофинанс", "факторинг",
)


# ════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════════════════

def _rand_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer":         "https://fedresurs.ru/",
        "Origin":          "https://fedresurs.ru",
    }


def _make_transport() -> httpx.AsyncHTTPTransport | None:
    if not PROXIES:
        return None
    return httpx.AsyncHTTPTransport(proxy=random.choice(PROXIES))


def _extract_inn(item: dict, fields: tuple[str, ...]) -> str:
    """Извлекает ИНН из указанных полей или текста."""
    for field in fields:
        val = str(item.get(field, "")).strip()
        if re.fullmatch(r"\d{10,12}", val):
            return val
    # Fallback: ищем в тексте
    text = " ".join(str(item.get(k, "")) for k in (
        "messageText", "text", "title", "description",
    ))
    m = INN_RE.search(text)
    return m.group(1) if m else ""


def _extract_lessee_inn(item: dict) -> str:
    """ИНН лизингополучателя (ПУТЬ А — майнеры)."""
    return _extract_inn(item, (
        "lesseeInn", "debtorInn", "entityInn",
        "participantInn", "companyInn", "inn",
    ))


def _extract_lessor_inn(item: dict) -> str:
    """ИНН лизингодателя (ПУТЬ Б — хостинги)."""
    return _extract_inn(item, (
        "lessorInn", "creditorInn", "lenderInn",
        "counterpartyInn", "ownerInn",
    ))


def _extract_lessor_meta(item: dict) -> tuple[str, str]:
    """Возвращает (ИНН лизингодателя, название лизингодателя)."""
    inn = _extract_lessor_inn(item)
    name = str(item.get(
        "lessorName",
        item.get("creditorName", item.get("counterpartyName", ""))
    )).strip()
    return inn, name


def _extract_lessor_okved(item: dict) -> str:
    """ОКВЭД лизингодателя если есть в ответе."""
    return str(item.get(
        "lessorOkved",
        item.get("creditorOkved", item.get("counterpartyOkved", ""))
    )).strip()


def _extract_leasing_text(item: dict) -> str:
    fields = [
        "messageText", "text", "title", "description",
        "subject", "contractSubject", "propertyDescription",
    ]
    parts = [str(item.get(f, "")) for f in fields if item.get(f)]
    return " ".join(parts)


def is_financial_lessor(okved: str, name: str) -> bool:
    """
    True если лизингодатель — финансовый посредник
    (банк, лизинговая компания, страховщик).
    Таких исключаем из ПУТИ Б.
    """
    okved = (okved or "").strip()
    name_lower = (name or "").lower()

    if any(okved.startswith(p) for p in FINANCIAL_OKVED_PREFIXES):
        return True
    if okved in FINANCIAL_OKVED_EXACT:
        return True
    if any(kw in name_lower for kw in FINANCIAL_NAME_KEYWORDS):
        return True
    # "лизинг" в названии без ОКВЭД 63.x — скорее всего финансовый
    if "лизинг" in name_lower and not okved.startswith("63."):
        return True
    return False


def classify_lessor(okved: str, name: str) -> str:
    """
    Классифицирует лизингодателя для скоринга.
    Возвращает: financial | datacenter | energy | realty | other
    """
    if is_financial_lessor(okved, name):
        return "financial"
    okved = okved or ""
    if okved.startswith("63."):
        return "datacenter"
    if okved.startswith("35."):
        return "energy"
    if okved.startswith("68."):
        return "realty"
    return "other"


def _params_for(endpoint: str, query: str, limit: int = 40, offset: int = 0) -> dict:
    page = offset // limit  # Федресурс использует page (0-based), не offset
    if "efrs-messages" in endpoint:
        return {"query": query, "limit": limit, "offset": offset, "page": page}
    elif "backend/search" in endpoint:
        return {"searchString": query, "limit": limit, "offset": offset, "page": page}
    else:
        return {"q": query, "limit": limit, "offset": offset, "page": page}


async def _probe_endpoint(client: httpx.AsyncClient, query: str) -> str | None:
    """Перебирает эндпоинты, возвращает рабочий."""
    for ep in FEDRESURS_ENDPOINTS:
        try:
            r = await client.get(
                ep,
                params=_params_for(ep, query, limit=1),
                headers=_headers(),
                timeout=20,
            )
            log.debug(f"Федресурс probe {ep}: HTTP {r.status_code}")
            if r.status_code == 200:
                return ep
        except Exception as e:
            log.debug(f"Федресурс {ep}: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Пагинированный сбор сообщений
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_pages(
    client: httpx.AsyncClient,
    query: str,
    endpoint: str,
    max_pages: int = 50,
) -> list[dict]:
    """
    Пагинированный обход сообщений по запросу (ИНН или ключевое слово).
    Останавливается при дублировании страниц или исчерпании результатов.
    """
    results: list[dict] = []
    seen_ids: set = set()       # дедупликация по id сообщения
    offset, limit = 0, 40

    for page_num in range(max_pages):
        for attempt in range(MAX_RETRIES):
            try:
                r = await client.get(
                    endpoint,
                    params=_params_for(endpoint, query, limit, offset),
                    headers=_headers(),
                    timeout=20,
                )
                if r.status_code == 429:
                    wait = 30 + random.uniform(0, 10)
                    log.warning(f"Федресурс 429 — ждём {wait:.0f}с")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code != 200:
                    log.debug(f"Федресурс HTTP {r.status_code} для '{query}'")
                    return results

                data = r.json()
                items: list[dict] = (
                    data.get("data") or data.get("items") or
                    data.get("content") or data.get("messages") or []
                )
                total = int(
                    data.get("total") or data.get("totalElements") or
                    data.get("count") or 0
                )

                if not items:
                    log.debug(f"Федресурс '{query}': страница {page_num} пуста — стоп")
                    return results

                # Дедупликация: стоп если все id на странице уже видели
                new_items = []
                for item in items:
                    item_id = (
                        item.get("id") or item.get("messageId") or
                        item.get("guid") or str(item)[:100]
                    )
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        new_items.append(item)

                if not new_items:
                    log.debug(f"Федресурс '{query}': страница {page_num} — все дубли, стоп")
                    return results

                results.extend(new_items)
                log.debug(f"Федресурс '{query}': страница {page_num}, +{len(new_items)} новых")

                offset += limit

                if total and offset >= total:
                    log.debug(f"Федресурс '{query}': достигли total={total}")
                    return results

                await asyncio.sleep(_rand_delay())
                break

            except Exception as e:
                log.debug(f"Федресурс attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

    return results


# ════════════════════════════════════════════════════════════════════════════
# ПУТЬ А + Б: поиск по ключевым словам
# ════════════════════════════════════════════════════════════════════════════

async def search_by_keywords(
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """
    Ищет договоры по майнинговым ключевым словам.

    Возвращает:
        miner_inns:    dict[inn → list[leasing_texts]]  — ПУТЬ А (лизингополучатели)
        hosting_inns:  dict[inn → dict]                 — ПУТЬ Б (лизингодатели, не финансовые)
        contracts:     list[dict]                       — все найденные договоры
        stats:         dict                             — статистика
    """
    if keywords is None:
        keywords = MINING_SEARCH_KEYWORDS

    miner_inns:   dict[str, list[str]] = {}   # inn → [тексты договоров]
    hosting_inns: dict[str, dict] = {}         # inn → {name, okved, lessor_type}
    all_contracts: list[dict] = []

    stats = {
        "keywords_searched":   0,
        "contracts_found":     0,
        "lessees_found":       0,
        "lessors_found":       0,
        "lessors_financial":   0,
        "lessors_accepted":    0,
    }

    transport = _make_transport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
    ) as client:

        endpoint = await _probe_endpoint(client, keywords[0])
        if not endpoint:
            log.warning("Федресурс: ни один эндпоинт не доступен")
            return {
                "miner_inns":   miner_inns,
                "hosting_inns": hosting_inns,
                "contracts":    all_contracts,
                "stats":        stats,
                "error":        "no_endpoint",
            }

        log.info(f"Федресурс: используем эндпоинт {endpoint}")

        for keyword in keywords:
            log.info(f"Федресурс: поиск по '{keyword}'")
            stats["keywords_searched"] += 1

            items = await _fetch_pages(client, keyword, endpoint)
            stats["contracts_found"] += len(items)

            for item in items:
                msg_type = str(item.get("messageType", item.get("type", ""))).lower()
                is_leasing = (
                    "лизинг" in msg_type
                    or "leasing" in msg_type
                    or "аренда" in msg_type
                    or not msg_type  # тип неизвестен — берём всё
                )
                if not is_leasing:
                    continue

                text = _extract_leasing_text(item)

                # ── ПУТЬ А: лизингополучатель = майнер ──────────────────────
                lessee_inn = _extract_lessee_inn(item)
                if lessee_inn:
                    if lessee_inn not in miner_inns:
                        miner_inns[lessee_inn] = []
                        stats["lessees_found"] += 1
                    if text:
                        miner_inns[lessee_inn].append(text)

                # ── ПУТЬ Б: лизингодатель = потенциальный хостинг ───────────
                lessor_inn, lessor_name = _extract_lessor_meta(item)
                if lessor_inn:
                    stats["lessors_found"] += 1
                    lessor_okved = _extract_lessor_okved(item)
                    lessor_type  = classify_lessor(lessor_okved, lessor_name)

                    if lessor_type == "financial":
                        stats["lessors_financial"] += 1
                        log.debug(
                            f"Пропускаем финансового лизингодателя: "
                            f"{lessor_name} [{lessor_okved}]"
                        )
                    else:
                        stats["lessors_accepted"] += 1
                        if lessor_inn not in hosting_inns:
                            hosting_inns[lessor_inn] = {
                                "name":        lessor_name,
                                "okved":       lessor_okved,
                                "lessor_type": lessor_type,
                                "keywords":    [],
                            }
                        hosting_inns[lessor_inn]["keywords"].append(keyword)

                all_contracts.append({
                    "keyword":     keyword,
                    "lessee_inn":  lessee_inn,
                    "lessor_inn":  lessor_inn,
                    "lessor_name": lessor_name,
                    "lessor_type": lessor_type if lessor_inn else "",
                    "text":        text,
                })

            await asyncio.sleep(_rand_delay())

    log.info(
        f"Федресурс keyword-search: "
        f"договоров={stats['contracts_found']} | "
        f"майнеров={stats['lessees_found']} | "
        f"хостингов_принято={stats['lessors_accepted']} | "
        f"финансовых_отсеяно={stats['lessors_financial']}"
    )

    return {
        "miner_inns":   miner_inns,
        "hosting_inns": hosting_inns,
        "contracts":    all_contracts,
        "stats":        stats,
        "error":        None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Поиск по конкретному ИНН (обратная совместимость)
# ════════════════════════════════════════════════════════════════════════════

async def fetch_fedresurs(inn: str) -> dict[str, Any]:
    """
    Ищет лизинговые договоры для конкретного ИНН.
    Сначала пробует HTTP API, при неудаче — Playwright.
    """
    result: dict[str, Any] = {
        "inn":           inn,
        "leasing_texts": [],
        "raw_count":     0,
        "error":         None,
    }

    transport = _make_transport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
    ) as client:
        endpoint = await _probe_endpoint(client, inn)
        if endpoint:
            items = await _fetch_pages(client, inn, endpoint)
            result["raw_count"] = len(items)
            result["leasing_texts"] = [
                _extract_leasing_text(i) for i in items
                if _extract_leasing_text(i).strip()
            ]
            log.debug(f"Федресурс {inn}: {len(items)} сообщений via API")
            return result

    log.info(f"Федресурс {inn}: HTTP API недоступен, fallback на Playwright")
    return await _fetch_fedresurs_playwright(inn)


async def _fetch_fedresurs_playwright(inn: str) -> dict[str, Any]:
    """Playwright-fallback когда HTTP API недоступен."""
    result: dict[str, Any] = {
        "inn": inn, "leasing_texts": [], "raw_count": 0, "error": None,
    }
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["error"] = "playwright_not_installed"
        return result

    from config import PLAYWRIGHT_HEADLESS, PLAYWRIGHT_TIMEOUT_MS
    from pipeline.rusprofile import _build_proxy_cfg, _pick_proxy

    proxy_cfg = _build_proxy_cfg(_pick_proxy())
    leasing_texts: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=PLAYWRIGHT_HEADLESS, proxy=proxy_cfg)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ru-RU",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,css}",
            lambda r: r.abort(),
        )

        search_url = f"https://fedresurs.ru/search/entities?searchString={inn}"
        try:
            await page.goto(
                search_url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
        except Exception as e:
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(2.0, 4.0))

        company_link = None
        try:
            link_el = await page.query_selector(
                ".company-name a, .search-result a, h3 > a"
            )
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    company_link = (
                        href if href.startswith("http")
                        else "https://fedresurs.ru" + href
                    )
        except Exception:
            pass

        if not company_link:
            company_link = f"https://fedresurs.ru/company/{inn}"

        try:
            await page.goto(
                company_link, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
        except Exception as e:
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(2.0, 3.0))

        try:
            full_text = await page.inner_text("body")
            for line in full_text.splitlines():
                line = line.strip()
                if line and any(
                    kw in line.lower()
                    for kw in ["лизинг", "аренда", "leasing", "финансовая аренда"]
                ):
                    leasing_texts.append(line)
        except Exception as e:
            log.debug(f"Федресурс playwright текст {inn}: {e}")

        await browser.close()

    result["leasing_texts"] = leasing_texts
    result["raw_count"] = len(leasing_texts)
    log.debug(f"Федресурс playwright {inn}: {len(leasing_texts)} строк с лизингом")
    return result
