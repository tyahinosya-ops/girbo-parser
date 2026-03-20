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

BASE = "https://fedresurs.ru"
PAGE_SIZE = 40

# Эндпоинты в порядке приоритета: (метод, путь, параметр_поиска, доп_параметры)
# /backend/encumbrances — актуальный эндпоинт (подтверждён 19 марта 2026 через DevTools)
# Возвращает: {"pageData": [...], "found": N}
# weakSide: [{role: "Lessee"|"Lessor", inn: "...", name: "..."}]
FEDRESURS_ENDPOINTS: list[tuple[str, str, str, dict]] = [
    ("GET", "/backend/encumbrances",   "searchString", {}),  # ← актуальный
    # Резервные (старые или альтернативные)
    ("GET",  "/api/sfacts",            "searchString", {}),
    ("GET",  "/api/v1/sfacts",         "searchString", {}),
    ("GET",  "/backend/sfacts",        "searchString", {}),
    ("POST", "/backend/sfacts",        "searchString", {}),
]

LEASING_MESSAGE_TYPES = [
    "ФинансоваяАренда", "Leasing", "LeasingContract", "УведомлениеОЛизинге",
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


def _headers(xsrf: str = "") -> dict:
    # sec-ch-ua Client Hints + Sec-Fetch-* + HTTP/1.1 — ключ к обходу Qrator
    # Подтверждено test_fedresurs.py 20.03.2026: HTTP 200, found=153 для antminer
    h = {
        "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept":             "application/json, text/plain, */*",
        "Accept-Language":    "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer":            BASE + "/encumbrances",
        "Origin":             BASE,
        "sec-ch-ua":          '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site":     "same-origin",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Dest":     "empty",
        "Cache-Control":      "no-cache",
        "Pragma":             "no-cache",
    }
    if xsrf:
        h["X-XSRF-TOKEN"]     = xsrf
        h["X-Requested-With"] = "XMLHttpRequest"
    return h


def _make_transport() -> httpx.AsyncHTTPTransport | None:
    if not PROXIES:
        return None
    return httpx.AsyncHTTPTransport(proxy=random.choice(PROXIES))


async def _init_session(client: httpx.AsyncClient) -> str:
    """Открывает главную страницу Федресурса — получает XSRF-TOKEN."""
    try:
        r = await client.get(
            BASE + "/",
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=20,
        )
        xsrf = client.cookies.get("XSRF-TOKEN", "")
        log.info(f"Федресурс сессия: HTTP {r.status_code}, XSRF={'да' if xsrf else 'нет'}")
        return xsrf
    except Exception as e:
        log.warning(f"Федресурс init_session: {e}")
        return ""


def _detect_items(data: dict | list) -> tuple[list, int]:
    """Извлекает items и total из ответа с любой структурой."""
    if isinstance(data, list):
        return data, len(data)
    if not isinstance(data, dict):
        return [], 0
    # Актуальная структура Федресурса: {pageData: [...], found: N}
    if "pageData" in data:
        items = data["pageData"] if isinstance(data["pageData"], list) else []
        total = int(data.get("found", len(items)))
        return items, total
    # Generic fallback
    for val in data.values():
        if isinstance(val, list) and len(val) > 0:
            total = 0
            for tkey in ("total", "totalCount", "totalElements", "count", "size", "found"):
                if data.get(tkey):
                    total = int(data[tkey])
                    break
            return val, total or len(val)
    for tkey in ("total", "totalCount", "totalElements", "count", "size", "found"):
        if data.get(tkey) and int(data[tkey]) > 0:
            return [], int(data[tkey])
    return [], 0


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


_LESSEE_ROLES = {"Lessee", "Debtor", "Borrower", "Pledgor"}
_LESSOR_ROLES = {"Lessor", "Creditor", "Lender", "Pledgee", "StrongSide", "Owner"}


def _extract_lessee_inn(item: dict) -> str:
    """ИНН лизингополучателя (ПУТЬ А — майнеры).
    Новая структура: weakSide[{role: 'Lessee', inn: '...'}]
    """
    for party in item.get("weakSide", []):
        if party.get("role") in _LESSEE_ROLES and not party.get("isHidden", False):
            inn = str(party.get("inn", "")).strip()
            if re.fullmatch(r"\d{10,12}", inn):
                return inn
    # Fallback: старые поля
    return _extract_inn(item, (
        "lesseeInn", "debtorInn", "entityInn", "participantInn", "companyInn", "inn",
    ))


def _extract_lessor_inn(item: dict) -> str:
    """ИНН лизингодателя (ПУТЬ Б — хостинги).
    Новая структура: strongSide[{role: 'Lessor', inn: '...'}]
    """
    # strongSide — основная сторона договора (Lessor / Creditor)
    for party in item.get("strongSide", []):
        if not party.get("isHidden", False):
            inn = str(party.get("inn", "")).strip()
            if re.fullmatch(r"\d{10,12}", inn):
                return inn
    # Fallback: weakSide с ролью лизингодателя
    for party in item.get("weakSide", []):
        if party.get("role") in _LESSOR_ROLES and not party.get("isHidden", False):
            inn = str(party.get("inn", "")).strip()
            if re.fullmatch(r"\d{10,12}", inn):
                return inn
    return _extract_inn(item, (
        "lessorInn", "creditorInn", "lenderInn", "counterpartyInn", "ownerInn",
    ))


def _extract_lessor_meta(item: dict) -> tuple[str, str]:
    """Возвращает (ИНН лизингодателя, название лизингодателя).
    Новая структура: strongSide[{role: 'Lessor', inn: '...', name: '...'}]
    """
    # strongSide — основная сторона (Lessor)
    for party in item.get("strongSide", []):
        if not party.get("isHidden", False):
            inn = str(party.get("inn", "")).strip()
            name = str(party.get("name", "")).strip()
            if re.fullmatch(r"\d{10,12}", inn):
                return inn, name
    # Fallback: weakSide с ролью лизингодателя
    for party in item.get("weakSide", []):
        if party.get("role") in _LESSOR_ROLES and not party.get("isHidden", False):
            inn = str(party.get("inn", "")).strip()
            name = str(party.get("name", "")).strip()
            if re.fullmatch(r"\d{10,12}", inn):
                return inn, name
    inn = _extract_lessor_inn(item)
    name = str(item.get(
        "lessorName", item.get("creditorName", item.get("counterpartyName", ""))
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


def _get_chrome_cookies() -> dict:
    """
    Читает cookies fedresurs.ru из двух источников (приоритет — файл):
      1. chrome_session.json — сохранён через save_chrome_session.py (надёжнее)
      2. browser-cookie3     — читает из Chrome напрямую (Chrome должен быть закрыт)
    """
    import pathlib

    # Источник 1: chrome_session.json (сохранён через save_chrome_session.py)
    session_file = pathlib.Path("chrome_session.json")
    if session_file.exists():
        try:
            import json
            data = json.loads(session_file.read_text(encoding="utf-8"))
            cookies = data.get("cookies", {})
            saved_headers = data.get("headers", {})
            if cookies:
                # Добавляем XSRF из заголовков если есть
                if "X-XSRF-TOKEN" in saved_headers and "XSRF-TOKEN" not in cookies:
                    cookies["XSRF-TOKEN"] = saved_headers["X-XSRF-TOKEN"]
                log.info(
                    f"Федресурс: загружена сессия из chrome_session.json "
                    f"({len(cookies)} cookies, XSRF={'да' if 'XSRF-TOKEN' in cookies else 'нет'})"
                )
                return cookies
        except Exception as e:
            log.debug(f"chrome_session.json: {e}")

    # Источник 2: browser-cookie3 (Chrome должен быть закрыт)
    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(domain_name="fedresurs.ru")
        cookies = {c.name: c.value for c in jar}
        if cookies:
            has_xsrf = "XSRF-TOKEN" in cookies
            log.info(
                f"Федресурс: Chrome cookies через browser-cookie3 "
                f"({len(cookies)} шт, XSRF={'да' if has_xsrf else 'нет'})"
            )
        return cookies
    except Exception as e:
        log.debug(f"browser_cookie3: {e}")
        return {}


async def _fetch_pages_with_cookies(
    cookies: dict, keywords: list[str], max_pages: int = 50
) -> dict[str, list[dict]]:
    """
    Использует cookies из Chrome для httpx-запросов к /backend/encumbrances.
    Qrator пропускает запросы с валидными сессионными cookies.
    """
    xsrf = cookies.get("XSRF-TOKEN", "")
    result: dict[str, list[dict]] = {}

    transport = _make_transport()
    async with httpx.AsyncClient(
        cookies=cookies,
        transport=transport,
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
        http2=False,
    ) as client:
        # Проверочный запрос
        try:
            r = await client.get(
                BASE + "/backend/encumbrances",
                params={"searchString": "antminer", "limit": 1, "offset": 0},
                headers=_headers(xsrf),
                timeout=10,
            )
            log.info(f"Федресурс Chrome-cookies probe: HTTP {r.status_code}")
            if r.status_code != 200:
                log.warning(f"Chrome cookies не работают (HTTP {r.status_code}) — cookies устарели?")
                return {}
        except Exception as e:
            log.debug(f"Chrome-cookies probe: {e}")
            return {}

        for keyword in keywords:
            log.info(f"Федресурс Chrome-cookies: поиск по '{keyword}'")
            items = await _fetch_pages(
                client, keyword,
                "GET", "/backend/encumbrances", "searchString", xsrf,
                max_pages=max_pages,
            )
            result[keyword] = items
            log.info(f"Федресурс Chrome-cookies '{keyword}': {len(items)} записей")
            await asyncio.sleep(_rand_delay())

    return result


async def _probe_endpoints(
    client: httpx.AsyncClient, xsrf: str
) -> tuple[str, str, str, dict] | None:
    """
    Перебирает эндпоинты, возвращает первый рабочий (метод, путь, параметр, доп_параметры).
    """
    for method, path, param, extra in FEDRESURS_ENDPOINTS:
        url = BASE + path
        try:
            if method == "GET":
                r = await client.get(
                    url,
                    params={"limit": 1, "offset": 0, param: "лизинг", **extra},
                    headers=_headers(xsrf),
                    timeout=20,
                )
            else:
                r = await client.post(
                    url,
                    json={"limit": 1, "offset": 0, param: "лизинг", **extra},
                    headers={**_headers(xsrf), "Content-Type": "application/json"},
                    timeout=20,
                )
            log.debug(f"Федресурс probe {method} {path}: HTTP {r.status_code}")
            if r.status_code != 200:
                continue
            try:
                data = r.json()
                items, total = _detect_items(data)
                if items or total:
                    log.info(f"Федресурс: рабочий эндпоинт {method} {path} ?{param}")
                    return method, path, param, extra
            except Exception:
                continue
        except Exception as e:
            log.debug(f"Федресурс probe {path}: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Пагинированный сбор сообщений
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_pages(
    client: httpx.AsyncClient,
    query: str,
    method: str,
    path: str,
    param: str,
    xsrf: str,
    extra: dict | None = None,
    max_pages: int = 50,
) -> list[dict]:
    """
    Пагинированный обход сообщений по запросу.
    Останавливается при дублировании или исчерпании результатов.
    """
    results: list[dict] = []
    seen_ids: set = set()
    offset = 0
    url = BASE + path
    extra = extra or {}

    for page_num in range(max_pages):
        for attempt in range(MAX_RETRIES):
            try:
                if method == "GET":
                    r = await client.get(
                        url,
                        params={"limit": PAGE_SIZE, "offset": offset, param: query, **extra},
                        headers=_headers(xsrf),
                        timeout=25,
                    )
                else:
                    r = await client.post(
                        url,
                        json={"limit": PAGE_SIZE, "offset": offset, param: query, **extra},
                        headers={**_headers(xsrf), "Content-Type": "application/json"},
                        timeout=25,
                    )

                if r.status_code == 429:
                    wait = 30 + random.uniform(0, 10)
                    log.warning(f"Федресурс 429 — ждём {wait:.0f}с")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code != 200:
                    log.debug(f"Федресурс HTTP {r.status_code} для '{query}'")
                    return results

                items, total = _detect_items(r.json())

                if not items:
                    log.debug(f"Федресурс '{query}': страница {page_num} пуста — стоп")
                    return results

                # Дедупликация
                new_items = []
                for item in items:
                    item_id = (
                        item.get("id") or item.get("messageId") or
                        item.get("guid") or item.get("number") or
                        f"{item.get('date','')}_{item.get('subject','')[:40]}"
                    )
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        new_items.append(item)

                if not new_items:
                    log.debug(f"Федресурс '{query}': стр.{page_num} — все дубли, стоп")
                    return results

                results.extend(new_items)
                log.debug(f"Федресурс '{query}': стр.{page_num} +{len(new_items)} (всего {len(results)})")

                offset += PAGE_SIZE
                if total and offset >= total:
                    return results

                await asyncio.sleep(_rand_delay())
                break

            except Exception as e:
                log.debug(f"Федресурс attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

    return results


# ════════════════════════════════════════════════════════════════════════════
# Playwright-стратегия: один браузер на все ключевые слова
# ════════════════════════════════════════════════════════════════════════════

async def _pw_fetch_keyword(page: Any, keyword: str, max_pages: int = 50) -> list[dict]:
    """
    Navigate-and-intercept через page.expect_response() — правильный Playwright-паттерн.
    Angular-SPA сама вызывает /backend/encumbrances при навигации на /encumbrances?searchString=...
    Это подтверждено intercept_log.json: antminer → реальные лизинговые данные.
    """
    import urllib.parse
    kw_enc = urllib.parse.quote(keyword)
    seen_ids: set = set()
    results: list[dict] = []

    for page_num in range(max_pages):
        offset = page_num * PAGE_SIZE

        spa_url = (
            f"{BASE}/encumbrances"
            f"?searchString={kw_enc}"
            f"&group=all&period=%7B%7D&additionalFnpSearch=true"
            f"&limit={PAGE_SIZE}&offset={offset}"
        )

        data: dict | None = None
        try:
            # expect_response ставим ДО goto — это правильный Playwright-паттерн
            async with page.expect_response(
                lambda r: (
                    "/backend/encumbrances" in r.url
                    and "searchString=" in r.url
                ),
                timeout=25_000,
            ) as resp_info:
                await page.goto(spa_url, wait_until="domcontentloaded", timeout=25_000)

            response = await resp_info.value
            log.debug(
                f"Playwright '{keyword}' стр.{page_num}: "
                f"перехвачен {response.url} → HTTP {response.status}"
            )
            if response.status == 200:
                data = await response.json()
            else:
                log.debug(f"Playwright '{keyword}' стр.{page_num}: API вернул {response.status}")
                break

        except Exception as e:
            log.debug(f"Playwright '{keyword}' стр.{page_num}: {e}")
            break

        if not data:
            break

        items, total = _detect_items(data)

        new_items = []
        for item in items:
            item_id = (
                item.get("number") or item.get("guid") or item.get("id") or
                f"{item.get('publishDate','')[:10]}_{item.get('type','')}"
            )
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                new_items.append(item)

        if not new_items:
            log.debug(f"Playwright '{keyword}' стр.{page_num}: пусто или дубли — стоп")
            break

        results.extend(new_items)
        log.debug(
            f"Playwright '{keyword}': стр.{page_num} "
            f"+{len(new_items)} (всего {len(results)}, total API={total})"
        )

        if total and (offset + PAGE_SIZE) >= total:
            break

        await asyncio.sleep(_rand_delay())

    return results


async def _search_all_keywords_playwright(
    keywords: list[str], max_pages: int = 50
) -> dict[str, list[dict]]:
    """
    Один браузер — один Qrator-challenge — все ключевые слова.
    Стратегии (по приоритету):
      1. CDP — подключаемся к уже запущенному Chrome (localhost:9222)
      2. channel="chrome" — запускаем установленный Google Chrome
      3. Chromium + stealth — запасной вариант
    Возвращает {keyword: [items...]}.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("playwright не установлен")
        return {}

    result: dict[str, list[dict]] = {}

    async with async_playwright() as pw:
        browser = None
        context = None
        via_cdp = False

        # ── Стратегия 1: подключение к уже запущенному Chrome (CDP) ──────────
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222", timeout=10_000)
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            via_cdp = True
            log.info("Playwright: подключились к реальному Chrome через CDP (порт 9222)")
        except Exception:
            browser = None

        # ── Стратегия 2: запуск установленного Google Chrome ─────────────────
        if not browser:
            try:
                browser = await pw.chromium.launch(
                    headless=False,
                    channel="chrome",   # реальный Chrome, не Chromium
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    locale="ru-RU", timezone_id="Europe/Moscow",
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                )
                log.info("Playwright: запущен реальный Google Chrome (channel=chrome)")
            except Exception as e:
                log.debug(f"Playwright channel=chrome недоступен: {e}")
                browser = None

        # ── Стратегия 3: Chromium со stealth-настройками ─────────────────────
        if not browser:
            browser = await pw.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                ],
            )
            context = await browser.new_context(
                locale="ru-RU", timezone_id="Europe/Moscow",
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
            )
            log.info("Playwright: запущен Chromium (stealth mode)")

        # Stealth init-script (для стратегий 2 и 3)
        if not via_cdp:
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru']});
                window.chrome = { runtime: {} };
            """)

        page = await context.new_page()

        # Прогрев: если не CDP — нужно пройти Qrator с нуля
        if not via_cdp:
            log.info(f"Playwright: загружаем {BASE}/ (Qrator challenge) ...")
            try:
                await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(5)
            except Exception as e:
                log.warning(f"Playwright homepage: {e}")
                await browser.close()
                return {}

            if "qrerror" in page.url or "403" in str(page.url):
                log.warning("Playwright: Qrator вернул 403")
                await browser.close()
                return {}

        log.info(f"Playwright: сессия готова ({page.url}), обрабатываем {len(keywords)} ключевых слов")

        for keyword in keywords:
            log.info(f"Playwright: поиск по '{keyword}'")
            items = await _pw_fetch_keyword(page, keyword, max_pages)
            result[keyword] = items
            log.info(f"Playwright '{keyword}': найдено {len(items)} записей")
            await asyncio.sleep(_rand_delay())

        await browser.close()

    return result


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

    keyword_items: dict[str, list[dict]] = {}  # keyword → items

    # ── Стратегия 0: cookies из реального Chrome (самая надёжная) ────────────
    # Qrator не блокирует запросы с cookies реального Chrome
    chrome_cookies = _get_chrome_cookies()
    if chrome_cookies:
        log.info("Федресурс: найдены Chrome cookies → пробуем без Playwright")
        kw_result = await _fetch_pages_with_cookies(chrome_cookies, keywords)
        if kw_result:
            keyword_items.update(kw_result)
            log.info(f"Федресурс: Chrome cookies сработали ({len(kw_result)} ключевых слов)")

    # ── Стратегия 1: httpx с Client Hints (sec-ch-ua) ────────────────────────
    # Qrator пропускает запросы с правильными Client Hints без cookies/Playwright
    missing_after_cookies = [kw for kw in keywords if not keyword_items.get(kw)]
    if missing_after_cookies:
        transport = _make_transport()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
            http2=False,
        ) as client:
            # Проверяем что Client Hints работают
            try:
                probe = await client.get(
                    BASE + "/backend/encumbrances",
                    params={"limit": 1, "offset": 0},
                    headers=_headers(),
                    timeout=10,
                )
                log.info(f"Федресурс httpx probe (Client Hints): HTTP {probe.status_code}")
                if probe.status_code == 200:
                    log.info("Федресурс: httpx работает с Client Hints!")
                    for keyword in missing_after_cookies:
                        log.info(f"Федресурс httpx: поиск по '{keyword}'")
                        items = await _fetch_pages(
                            client, keyword,
                            "GET", "/backend/encumbrances", "searchString", xsrf="",
                        )
                        keyword_items[keyword] = items
                else:
                    log.info(f"Федресурс httpx: {probe.status_code} → Playwright")
            except Exception as e:
                log.debug(f"Федресурс httpx probe: {e}")

    # ── Стратегия 2: Playwright (один браузер, navigate-and-intercept) ────────
    # kw not in keyword_items: только НЕ обработанные ([], тоже считается обработанным)
    missing = [kw for kw in keywords if kw not in keyword_items]
    if missing:
        log.info(f"Федресурс: Playwright для {len(missing)} ключевых слов")
        pw_results = await _search_all_keywords_playwright(missing)
        keyword_items.update(pw_results)

    # Обрабатываем все собранные записи
    for keyword in keywords:
        items = keyword_items.get(keyword, [])
        stats["keywords_searched"] += 1
        stats["contracts_found"] += len(items)

        for item in items:
                msg_type = str(item.get("messageType", item.get("type", ""))).lower()
                # Принимаем все типы договоров с лизингом/залогом/арендой
                # "ChangeFinancialLeaseContract2", "StartFinancialLeaseContract", etc.
                is_leasing = (
                    "lease" in msg_type       # ChangeFinancialLeaseContract2, etc.
                    or "лизинг" in msg_type
                    or "leasing" in msg_type
                    or "аренда" in msg_type
                    or "pledge" in msg_type   # залоги тоже учитываем
                    or "залог" in msg_type
                    or not msg_type           # тип неизвестен — берём всё
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
                                "texts":       [],
                            }
                        hosting_inns[lessor_inn]["keywords"].append(keyword)
                        if text:
                            hosting_inns[lessor_inn]["texts"].append(text)

                all_contracts.append({
                    "keyword":     keyword,
                    "lessee_inn":  lessee_inn,
                    "lessor_inn":  lessor_inn,
                    "lessor_name": lessor_name,
                    "lessor_type": lessor_type if lessor_inn else "",
                    "text":        text,
                })

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
        http2=False,
    ) as client:
        xsrf = await _init_session(client)
        endpoint = await _probe_endpoints(client, xsrf)
        if endpoint:
            method, path, param, extra = endpoint
            items = await _fetch_pages(client, inn, method, path, param, xsrf, extra)
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
