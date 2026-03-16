"""
collector.py — Этап 1: сбор ИНН через ЕГРЮЛ (async httpx)
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import AsyncIterator

import httpx

from config import (
    REQUEST_DELAY_MIN, REQUEST_DELAY_MAX,
    TARGET_OKVEDS, TARGET_REGIONS, USER_AGENTS,
    MAX_RETRIES,
)

log = logging.getLogger(__name__)

EGRUL_BASE = "https://egrul.nalog.ru"


def _rand_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin":          EGRUL_BASE,
        "Referer":         EGRUL_BASE + "/",
    }


async def _init_egrul_session(client: httpx.AsyncClient) -> None:
    """Открываем главную страницу — получаем cookies."""
    try:
        r = await client.get(
            EGRUL_BASE + "/",
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=15,
            follow_redirects=True,
        )
        log.debug(f"ЕГРЮЛ init: HTTP {r.status_code}, cookies={list(r.cookies.keys())}")
    except Exception as e:
        log.warning(f"ЕГРЮЛ init ошибка: {e}")
    await asyncio.sleep(random.uniform(1.5, 3.0))


async def _get_token(client: httpx.AsyncClient, region: str, okvd: str) -> str | None:
    """POST на egrul.nalog.ru — получаем поисковый токен."""
    okvd_clean = okvd.replace(".", "")
    payloads = [
        {"query": "", "region": region, "okvedCodes": okvd_clean, "vo": "ul"},
        {"query": "", "region": region, "okvedCodes": okvd,       "vo": "ul"},
        {"query": "", "region": region, "okvedCodes": okvd_clean, "vo": ""},
    ]
    for attempt in range(MAX_RETRIES):
        for payload in payloads:
            try:
                r = await client.post(
                    EGRUL_BASE + "/",
                    data=payload,
                    headers={
                        **_headers(),
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                    timeout=15,
                )
                if r.status_code == 200:
                    token = r.json().get("t")
                    if token:
                        log.debug(f"ЕГРЮЛ token: {region}/{okvd} → {token[:20]}…")
                        return token
            except Exception as e:
                log.debug(f"ЕГРЮЛ token attempt {attempt}: {e}")
            await asyncio.sleep(random.uniform(0.5, 1.5))

        backoff = 2 ** attempt
        log.debug(f"ЕГРЮЛ token retry backoff {backoff}s")
        await asyncio.sleep(backoff)

    log.warning(f"ЕГРЮЛ: нет токена для {region}/{okvd}")
    return None


async def _fetch_page(
    client: httpx.AsyncClient, token: str, page: int
) -> tuple[list[dict], int]:
    """GET /search-result → (строки, всего записей)."""
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                EGRUL_BASE + "/search-result",
                params={"t": token, "r": "y", "p": page},
                headers=_headers(),
                timeout=15,
            )
            r.raise_for_status()
            data  = r.json()
            rows  = data.get("rows", [])
            total = int(data.get("cnt", len(rows)) or 0)
            return rows, total
        except Exception as e:
            log.debug(f"ЕГРЮЛ page {page} attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)

    return [], 0


async def collect_inns_egrul(
    okveds: list[str] | None = None,
    regions: list[str] | None = None,
    proxies: list[str] | None = None,
) -> list[str]:
    """
    Собирает ИНН по списку ОКВЭД × регионов.
    Возвращает дедуплицированный список строк.
    """
    okveds  = okveds  or TARGET_OKVEDS
    regions = regions or TARGET_REGIONS

    # Настройка прокси (берём случайный если есть)
    proxy_url = random.choice(proxies) if proxies else None
    transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None

    seen: set[str] = set()

    async with httpx.AsyncClient(
        transport=transport,
        cookies={},
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:
        await _init_egrul_session(client)

        for okvd in okveds:
            for region in regions:
                log.info(f"ЕГРЮЛ: ОКВЭД {okvd} / регион {region}")
                token = await _get_token(client, region, okvd)
                if not token:
                    continue

                await asyncio.sleep(random.uniform(0.8, 1.5))

                rows, total = await _fetch_page(client, token, page=1)
                log.info(f"  total={total}, стр.1={len(rows)}")

                for row in rows:
                    inn = str(row.get("i", "")).strip()
                    if inn and _valid_inn(inn):
                        seen.add(inn)

                total_pages = min((total // 20) + 1, 10)
                for pg in range(2, total_pages + 1):
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    more, _ = await _fetch_page(client, token, pg)
                    if not more:
                        break
                    for row in more:
                        inn = str(row.get("i", "")).strip()
                        if inn and _valid_inn(inn):
                            seen.add(inn)

                await asyncio.sleep(random.uniform(2.0, 4.0))

    result = list(seen)
    log.info(f"ЕГРЮЛ итого: {len(result)} уникальных ИНН")
    return result


def _valid_inn(inn: str) -> bool:
    return inn.isdigit() and len(inn) in (10, 12)
