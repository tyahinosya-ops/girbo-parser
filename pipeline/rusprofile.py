"""
rusprofile.py — парсер RusProfile через Playwright (JS-рендеринг)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

from config import (
    PLAYWRIGHT_HEADLESS, PLAYWRIGHT_TIMEOUT_MS,
    REQUEST_DELAY_MIN, REQUEST_DELAY_MAX,
    USER_AGENTS, PROXIES,
)

log = logging.getLogger(__name__)

# Финансовые ключевые слова для поиска в тексте страницы
_ELECTRICITY_KW = [
    "электроэнерги", "электр. энерг", "э/э",
    "коммунальные услуги", "потребление энергии", "энергоносител",
]


def _rand_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def _pick_proxy() -> str | None:
    return random.choice(PROXIES) if PROXIES else None


def _parse_number(s: str) -> float | None:
    s = re.sub(r"[\s\xa0]", "", str(s)).replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


async def fetch_rusprofile(inn: str) -> dict[str, Any]:
    """
    Загружает страницу компании с rusprofile.ru через Playwright.
    Возвращает словарь с финансовыми данными.
    """
    result: dict[str, Any] = {
        "inn":               inn,
        "name":              "",
        "revenue":           0.0,
        "fixed_assets":      0.0,
        "balance_total":     0.0,
        "net_profit":        0.0,
        "employees":         0,
        "electricity":       0.0,
        "cost_of_sales":     0.0,
        "status":            "",
        "error":             None,
    }

    try:
        from playwright.async_api import async_playwright, Error as PWError
    except ImportError:
        log.error("playwright не установлен. Запусти: pip install playwright && playwright install chromium")
        result["error"] = "playwright_not_installed"
        return result

    proxy_url = _pick_proxy()
    proxy_cfg  = None
    if proxy_url:
        # Playwright принимает proxy в виде {"server": "http://host:port", ...}
        m = re.match(r"(?P<scheme>https?://)(?:(?P<user>[^:]+):(?P<pwd>[^@]+)@)?(?P<host>.+)", proxy_url)
        if m:
            proxy_cfg = {"server": m.group("scheme") + m.group("host")}
            if m.group("user"):
                proxy_cfg["username"] = m.group("user")
                proxy_cfg["password"] = m.group("pwd") or ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            proxy=proxy_cfg,
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ru-RU",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # Блокируем лишние ресурсы для скорости
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,css}",
            lambda r: r.abort()
        )

        search_url = f"https://www.rusprofile.ru/search?query={inn}&type=ul"
        try:
            await page.goto(search_url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            log.warning(f"RusProfile goto failed для ИНН {inn}: {e}")
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(1.5, 3.0))

        # ── Находим ссылку на страницу компании ────────────────────────────
        company_link = None
        try:
            link_el = await page.query_selector("a.company-name, .org-name a, h3 > a")
            if link_el:
                company_link = await link_el.get_attribute("href")
                if company_link and not company_link.startswith("http"):
                    company_link = "https://www.rusprofile.ru" + company_link
        except Exception:
            pass

        if not company_link:
            log.debug(f"RusProfile: нет ссылки на компанию для ИНН {inn}")
            result["error"] = "no_company_link"
            await browser.close()
            return result

        await asyncio.sleep(_rand_delay())

        try:
            await page.goto(company_link, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            log.warning(f"RusProfile company page failed для ИНН {inn}: {e}")
            result["error"] = str(e)
            await browser.close()
            return result

        await asyncio.sleep(random.uniform(2.0, 4.0))

        # ── Название ────────────────────────────────────────────────────────
        try:
            name_el = await page.query_selector("h1.company-name, h1[itemprop='name']")
            if name_el:
                result["name"] = (await name_el.inner_text()).strip()
        except Exception:
            pass

        # ── Статус ──────────────────────────────────────────────────────────
        try:
            status_el = await page.query_selector(".company-status, .status-label")
            if status_el:
                result["status"] = (await status_el.inner_text()).strip()
        except Exception:
            pass

        # ── Финансы из таблицы ──────────────────────────────────────────────
        try:
            rows = await page.query_selector_all("table.finance-table tr, .finances-row, tr")
            for row in rows:
                text = (await row.inner_text()).lower().strip()

                # Выручка
                if "выручка" in text and result["revenue"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and val > 0:
                            result["revenue"] = val * 1000
                            break

                # Себестоимость
                if "себестоимость" in text and result["cost_of_sales"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and val > 0:
                            result["cost_of_sales"] = val * 1000
                            break

                # Основные средства
                if ("основные средства" in text or "внеоборотные активы" in text) \
                        and result["fixed_assets"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and val > 0:
                            result["fixed_assets"] = val * 1000
                            break

                # Валюта баланса
                if ("баланс" in text or "валюта баланса" in text) \
                        and result["balance_total"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and val > 0:
                            result["balance_total"] = val * 1000
                            break

                # Чистая прибыль
                if "чистая прибыль" in text and result["net_profit"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and val > 0:
                            result["net_profit"] = val * 1000
                            break

                # Расходы на ЭЭ (поиск по ключевым словам)
                if any(kw in text for kw in _ELECTRICITY_KW) \
                        and result["electricity"] == 0.0:
                    nums = re.findall(r"[\d\s\xa0]+", text)
                    for n in nums:
                        val = await _parse_number(n)
                        if val and 100 < val * 1000 < 1e13:
                            result["electricity"] = val * 1000
                            break

        except Exception as e:
            log.debug(f"RusProfile финансы {inn}: {e}")

        # ── Среднесписочная численность ─────────────────────────────────────
        try:
            full_text = await page.inner_text("body")
            emp_m = re.search(
                r"(?:среднесписочная|численность сотрудников)[^\d]*(\d+)",
                full_text, re.IGNORECASE
            )
            if emp_m:
                result["employees"] = int(emp_m.group(1))
        except Exception:
            pass

        await browser.close()

    log.debug(f"RusProfile {inn}: revenue={result['revenue']:.0f}, FA={result['fixed_assets']:.0f}")
    return result
