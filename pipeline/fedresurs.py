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
    "https://fedresurs.ru/backend/search",
    "https://fedresurs.ru/backend/efrs-messages",
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


async def _probe_endpoint(
    client: httpx.AsyncClient, inn: str
) -> str | None:
    """Перебирает эндпоинты, возвращает рабочий."""
    for ep in FEDRESURS_ENDPOINTS:
        try:
            r = await client.get(
                ep,
                params={"searchString": inn, "limit": 1, "offset": 0},
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
                params: dict[str, Any] = {
                    "searchString": inn,
                    "limit": limit,
                    "offset": offset,
                }
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


async def fetch_fedresurs(inn: str) -> dict[str, Any]:
    """
    Ищет лизинговые договоры для ИНН на Федресурсе.
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
        if not endpoint:
            log.warning(f"Федресурс: нет рабочего endpoint для ИНН {inn}")
            result["error"] = "no_endpoint"
            return result

        items = await _fetch_leasing_by_inn(client, inn, endpoint)
        result["raw_count"] = len(items)

        texts = [i["text"] for i in items if i["text"].strip()]
        result["leasing_texts"] = texts

        log.debug(f"Федресурс {inn}: {len(items)} сообщений, {len(texts)} с текстом")

    return result
