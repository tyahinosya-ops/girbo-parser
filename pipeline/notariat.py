"""
notariat.py — проверка залогов движимого имущества на reestr-zalogov.ru

Реестр ведёт Федеральная нотариальная палата (ФНП).
Залог ASIC-оборудования = сильный сигнал майнинга.

API: https://www.reestr-zalogov.ru/search/index
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from config import USER_AGENTS, PROXIES, MAX_RETRIES, REQUEST_DELAY_MIN, REQUEST_DELAY_MAX

log = logging.getLogger(__name__)

NOTARIAT_BASE = "https://www.reestr-zalogov.ru"

# Ключевые слова ASIC/майнинг в описании залога
ASIC_KEYWORDS: tuple[str, ...] = (
    "antminer", "whatsminer", "bitmain", "microbt",
    "асик", "asic", "майнер", "miner",
    "innosilicon", "goldshell", "jasminer", "ebang",
    "avalon", "canaan", "ibelink",
    "оборудование для майнинга", "майнинговое оборудование",
    "добыча криптовалют", "криптовалют",
)

# Широкие слова — принимаем только с подтверждением
BROAD_KEYWORDS: tuple[str, ...] = ("эвм", "вычислительное", "серверное")
BROAD_CONFIRM:  tuple[str, ...] = ("майнинг", "крипто", "asic", "асик", "bitcoin", "btc")


def _rand_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/html, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer":         f"{NOTARIAT_BASE}/search/index",
        "X-Requested-With": "XMLHttpRequest",
    }


def _make_transport() -> httpx.AsyncHTTPTransport | None:
    if not PROXIES:
        return None
    return httpx.AsyncHTTPTransport(proxy=random.choice(PROXIES))


def _is_asic_pledge(text: str) -> bool:
    """Определяет, относится ли залог к майнинговому оборудованию."""
    text_lower = text.lower()

    # Точные ключевые слова — сразу да
    for kw in ASIC_KEYWORDS:
        if kw in text_lower:
            return True

    # Широкие слова + подтверждающие
    for broad in BROAD_KEYWORDS:
        if broad in text_lower:
            for confirm in BROAD_CONFIRM:
                if confirm in text_lower:
                    return True

    return False


async def _get_csrf_token(client: httpx.AsyncClient) -> str:
    """Получает CSRF-токен с главной страницы реестра."""
    try:
        r = await client.get(
            f"{NOTARIAT_BASE}/search/index",
            headers={**_headers(), "Accept": "text/html,*/*"},
            timeout=15,
        )
        # Ищем _csrf в HTML
        m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)', r.text)
        if m:
            return m.group(1)
        # Fallback: из куки
        for key, val in r.cookies.items():
            if "csrf" in key.lower():
                return val
    except Exception as e:
        log.debug(f"Нотариат csrf: {e}")
    return ""


async def _search_pledges(
    client: httpx.AsyncClient,
    inn: str,
    csrf: str,
) -> list[dict]:
    """
    Ищет залоги по ИНН должника.
    Возвращает список записей из реестра.
    """
    pledges: list[dict] = []

    # Нотариат поддерживает поиск по ИНН залогодателя (должника)
    payload = {
        "SearchForm[type]":              "2",      # поиск по ИНН юрлица
        "SearchForm[debtorInn]":         inn,
        "SearchForm[debtorType]":        "2",      # юридическое лицо
        "_csrf":                         csrf,
    }

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.post(
                f"{NOTARIAT_BASE}/search/index",
                data=payload,
                headers=_headers(),
                timeout=20,
            )

            if r.status_code == 429:
                wait = 30 + random.uniform(0, 10)
                log.warning(f"Нотариат 429 — ждём {wait:.0f}с")
                await asyncio.sleep(wait)
                continue

            if r.status_code != 200:
                log.debug(f"Нотариат {inn}: HTTP {r.status_code}")
                return pledges

            # Пробуем JSON
            try:
                data = r.json()
                items = (
                    data.get("items") or data.get("data") or
                    data.get("records") or []
                )
                if isinstance(items, list):
                    pledges.extend(items)
                    return pledges
            except Exception:
                pass

            # Fallback: парсим HTML
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")

            for row in soup.select(".search-result-item, .pledge-item, tr.record"):
                text = row.get_text(" ", strip=True)
                if text:
                    pledges.append({
                        "raw_text":      text,
                        "reg_number":    _extract_field(row, "reg_number", "Рег. №"),
                        "reg_date":      _extract_field(row, "reg_date",   "Дата"),
                        "property_desc": _extract_field(row, "property",   "Предмет"),
                        "pledgor_name":  _extract_field(row, "pledgor",    "Залогодатель"),
                    })
            return pledges

        except Exception as e:
            log.debug(f"Нотариат {inn} attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)

    return pledges


def _extract_field(row: Any, css_class: str, label: str) -> str:
    """Извлекает поле из HTML-строки по классу или тексту метки."""
    try:
        el = row.select_one(f".{css_class}")
        if el:
            return el.get_text(strip=True)
        # Fallback: ищем по метке
        text = row.get_text(" ", strip=True)
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*(.+?)(?:\s{{2,}}|$)", text)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


async def fetch_notariat(inn: str) -> dict[str, Any]:
    """
    Проверяет ИНН в реестре залогов нотариата.

    Возвращает:
        pledges:        list[dict]  — найденные записи о залогах
        has_asic_pledge: bool       — есть ли залог ASIC/майнинг оборудования
        pledge_count:   int         — общее кол-во залогов
        error:          str | None
    """
    result: dict[str, Any] = {
        "inn":            inn,
        "pledges":        [],
        "has_asic_pledge": False,
        "pledge_count":   0,
        "error":          None,
    }

    transport = _make_transport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:
        try:
            await asyncio.sleep(_rand_delay())

            csrf = await _get_csrf_token(client)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            pledges = await _search_pledges(client, inn, csrf)
            result["pledges"]      = pledges
            result["pledge_count"] = len(pledges)

            # Проверяем каждый залог на ASIC-ключевые слова
            for pledge in pledges:
                text = (
                    pledge.get("property_desc", "") + " " +
                    pledge.get("raw_text", "")
                )
                if _is_asic_pledge(text):
                    result["has_asic_pledge"] = True
                    break

            if pledges:
                log.debug(
                    f"Нотариат {inn}: {len(pledges)} залогов, "
                    f"ASIC={result['has_asic_pledge']}"
                )

        except Exception as e:
            log.debug(f"fetch_notariat {inn}: {e}")
            result["error"] = str(e)

    return result
