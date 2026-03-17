"""
fedresurs.py — async поиск лизинговых договоров на Федресурсе
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
    # Актуальный публичный API (2024-2025)
    "https://fedresurs.ru/backend/efrs-messages",
    # Fallback — старый backend
    "https://fedresurs.ru/backend/search",
    # Fallback — REST API
    "https://fedresurs.ru/api/messages",
]

# Типы сообщений, связанные с лизингом
LEASING_MESSAGE_TYPES = [
    "ФинансоваяАренда",
    "Leasing",
    "LeasingContract",
    "УведомлениеОЛизинге",
]


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


def _extract_inn(item: dict) -> str:
    """Пытается вытащить ИНН из разных полей ответа."""
    for field in ("entityInn", "inn", "companyInn", "participantInn", "debtorInn"):
        val = str(item.get(field, "")).strip()
        if re.fullmatch(r"\d{10,12}", val):
            return val
    text = " ".join(
        str(item.get(k, ""))
        for k in ("messageText", "text", "title", "description", "entityName", "debtorName")
    )
    m = INN_RE.search(text)
    return m.group(1) if m else ""


def _extract_leasing_text(item: dict) -> str:
    """Собирает весь текстовый контент из сообщения."""
    fields = ["messageText", "text", "title", "description", "subject",
              "contractSubject", "propertyDescription"]
    parts = [str(item.get(f, "")) for f in fields if item.get(f)]
    return " ".join(parts)


def _params_for(endpoint: str, inn: str, limit: int = 1, offset: int = 0) -> dict:
    """Возвращает параметры запроса под конкретный endpoint."""
    if "efrs-messages" in endpoint:
        # Актуальный API: параметр "query"
        return {"query": inn, "limit": limit, "offset": offset}
    elif "backend/search" in endpoint:
        return {"searchString": inn, "limit": limit, "offset": offset}
    else:
        return {"inn": inn, "limit": limit, "offset": offset}


async def _probe_endpoint(
    client: httpx.AsyncClient, inn: str
) -> str | None:
    """Перебирает эндпоинты, возвращает рабочий."""
    for ep in FEDRESURS_ENDPOINTS:
        try:
            r = await client.get(
                ep,
                params=_params_for(ep, inn),
                headers=_headers(),
                timeout=20,
            )
            log.debug(f"Федресурс probe {ep}: HTTP {r.status_code}")
            if r.status_code == 200:
                return ep
        except Exception as e:
            log.debug(f"Федресурс {ep}: {e}")
    return None


async def _fetch_leasing_by_inn(
    client: httpx.AsyncClient,
    inn: str,
    endpoint: str,
) -> list[dict]:
    """
    Пагинированный обход сообщений по конкретному ИНН.
    """
    results: list[dict] = []
    offset, limit = 0, 40

    while offset < 400:
        for attempt in range(MAX_RETRIES):
            try:
                params: dict[str, Any] = _params_for(endpoint, inn, limit, offset)
                r = await client.get(
                    endpoint,
                    params=params,
                    headers=_headers(),
                    timeout=20,
                )
                if r.status_code == 429:
                    wait = 30 + random.uniform(0, 10)
                    log.warning(f"Федресурс 429 — ждём {wait:.0f}с")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code != 200:
                    log.debug(f"Федресурс HTTP {r.status_code}")
                    return results

                data  = r.json()
                items = (
                    data.get("data") or data.get("items") or
                    data.get("content") or data.get("messages") or []
                )
                total = int(
                    data.get("total") or data.get("totalElements") or
                    data.get("count") or 0
                )

                if not items:
                    return results

                for item in items:
                    # Проверяем тип — нам нужен только лизинг
                    msg_type = str(item.get("messageType", item.get("type", ""))).lower()
                    is_leasing = (
                        "лизинг" in msg_type
                        or "leasing" in msg_type
                        or "аренда" in msg_type
                        or any(t.lower() in msg_type for t in LEASING_MESSAGE_TYPES)
                    )
                    # Если тип неизвестен — берём всё (текст проверим в фильтрах)
                    results.append({
                        "inn":         _extract_inn(item),
                        "text":        _extract_leasing_text(item),
                        "is_leasing":  is_leasing,
                        "raw":         item,
                    })

                offset += limit
                if total and offset >= total:
                    return results

                await asyncio.sleep(_rand_delay())
                break  # успешная итерация — выходим из retry loop

            except Exception as e:
                log.debug(f"Федресурс attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

    return results


async def _fetch_fedresurs_playwright(inn: str) -> dict[str, Any]:
    """
    Playwright-fallback: загружает страницу компании на fedresurs.ru через браузер.
    Используется когда HTTP API недоступен (бан, капча, авторизация).
    """
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
        await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,css}", lambda r: r.abort())

        search_url = f"https://fedresurs.ru/search/entities?searchString={inn}"
        try:
            await page.goto(search_url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Ищем ссылку на карточку компании
        company_link = None
        try:
            link_el = await page.query_selector(".company-name a, .search-result a, h3 > a")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    company_link = href if href.startswith("http") else "https://fedresurs.ru" + href
        except Exception:
            pass

        if not company_link:
            # Пробуем прямой URL по ИНН
            company_link = f"https://fedresurs.ru/company/{inn}"

        try:
            await page.goto(company_link, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(2.0, 3.0))

        # Ищем сообщения о лизинге в тексте страницы
        try:
            full_text = await page.inner_text("body")
            lines = [l.strip() for l in full_text.splitlines() if l.strip()]
            for line in lines:
                line_low = line.lower()
                if any(kw in line_low for kw in ["лизинг", "аренда", "leasing", "финансовая аренда"]):
                    leasing_texts.append(line)
        except Exception as e:
            log.debug(f"Федресурс playwright текст {inn}: {e}")

        await browser.close()

    result["leasing_texts"] = leasing_texts
    result["raw_count"] = len(leasing_texts)
    log.debug(f"Федресурс playwright {inn}: {len(leasing_texts)} строк с лизингом")
    return result


async def fetch_fedresurs(inn: str) -> dict[str, Any]:
    """
    Ищет лизинговые договоры для ИНН на Федресурсе.
    Сначала пробует HTTP API, при неудаче — Playwright.
    Возвращает: {"inn": ..., "leasing_texts": [...], "raw_count": N}
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
            items = await _fetch_leasing_by_inn(client, inn, endpoint)
            result["raw_count"] = len(items)
            result["leasing_texts"] = [i["text"] for i in items if i["text"].strip()]
            log.debug(f"Федресурс {inn}: {len(items)} сообщений via API")
            return result

    # HTTP API недоступен — пробуем Playwright
    log.info(f"Федресурс {inn}: HTTP API недоступен, fallback на Playwright")
    return await _fetch_fedresurs_playwright(inn)
