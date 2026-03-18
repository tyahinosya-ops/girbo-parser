"""
scoring.py — Этап 4: взвешенный скоринг компаний

Базовые веса:
    leasing_keywords  40%
    fixed_assets      25%
    electricity       20%
    revenue           15%

Бонусы (приоритизация, не жёсткий отсев):
    + регион с дешёвой ЭЭ      до +15 баллов
    + год регистрации (крипто-волны)  до +12 баллов
    + тип лизингодателя (хостинги)   до +20 баллов
    + перекрёст путей А и Б          +15 баллов
"""
from __future__ import annotations

import logging
import re
from typing import Any

from config import SCORE_WEIGHTS

log = logging.getLogger(__name__)

# ── Нормализационные максимумы ───────────────────────────────────────────────
_REVENUE_MAX      = 1_000_000_000   # 1 млрд руб
_FIXED_ASSETS_MAX = 500_000_000     # 500 млн руб
_ELECTRICITY_MAX  = 100_000_000     # 100 млн руб

# ── Пороговые бонусы (базовые) ───────────────────────────────────────────────
_BONUS_EE: list[tuple[float, int]] = [
    (50_000_000, 15),
    (20_000_000, 10),
    (10_000_000,  5),
]
_BONUS_FA: list[tuple[float, int]] = [
    (200_000_000, 10),
    ( 50_000_000,  5),
]
_BONUS_REV_PER_EMP: list[tuple[float, int]] = [
    (20_000_000, 10),
    (10_000_000,  5),
]

# ── Бонус: регионы с дешёвой электроэнергией ────────────────────────────────
# Ключ: подстрока в названии региона (lowercase)
_REGION_BONUSES: list[tuple[str, int]] = [
    ("иркутск",    15),   # ~1.5 руб/кВт·ч — самая дешёвая в РФ
    ("хакасия",    15),   # ~1.8 руб/кВт·ч
    ("красноярск", 12),   # ~2.0 руб/кВт·ч
    ("бурятия",    10),   # ~2.2 руб/кВт·ч
    ("карели",     10),   # ~2.3 руб/кВт·ч (ГЭС)
    ("кабардино",   8),   # ~2.5 руб/кВт·ч
    ("дагестан",    8),   # ~2.5 руб/кВт·ч (ГЭС)
    ("забайкаль",   7),
    ("амурск",      7),
    ("тыва",        6),
]

# ── Бонус: год регистрации (волны роста крипты) ──────────────────────────────
_REG_YEAR_BONUSES: dict[int, int] = {
    2017: 12,   # первый большой бум (BTC ~$20k)
    2018: 10,   # продолжение / дешёвое оборудование
    2020: 10,   # DeFi лето + начало роста
    2021: 12,   # BTC ATH $69k, легализация майнинга в РФ
    2024:  8,   # новый бум + закон о майнинге в РФ
    2025:  8,   # продолжение легализации
}

# ── Бонус: тип лизингодателя (ПУТЬ Б — хостинги) ────────────────────────────
_LESSOR_TYPE_BONUSES: dict[str, int] = {
    "datacenter": 20,   # ОКВЭД 63.x — прямое попадание
    "energy":     10,   # ОКВЭД 35.x — энергетика
    "realty":      5,   # ОКВЭД 68.x — недвижимость / возможно ЦОД
    "other":       2,   # Неизвестно, но не финансовый
    "financial":   0,   # Банк/лизинговая компания — не учитываем
}

# ── Бонус: перекрёст путей А и Б ────────────────────────────────────────────
_CROSS_PATH_BONUS = 15


# ════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════════════════

def _normalize(value: float, max_value: float) -> float:
    if max_value <= 0 or value <= 0:
        return 0.0
    return min(value / max_value, 1.0)


def _bonus(value: float, thresholds: list[tuple[float, int]]) -> int:
    for threshold, points in thresholds:
        if value >= threshold:
            return points
    return 0


def _calc_region_bonus(region: str) -> tuple[int, str]:
    """Бонус за регион с дешёвой электроэнергией."""
    if not region:
        return 0, ""
    region_lower = region.lower()
    for keyword, points in _REGION_BONUSES:
        if keyword in region_lower:
            return points, f"регион={region}(+{points})"
    return 0, ""


def _calc_reg_year_bonus(reg_date: str) -> tuple[int, str]:
    """Бонус за год регистрации совпадающий с волнами роста крипты."""
    if not reg_date:
        return 0, ""
    match = re.search(r"\b(20\d{2})\b", str(reg_date))
    if not match:
        return 0, ""
    year = int(match.group(1))
    points = _REG_YEAR_BONUSES.get(year, 0)
    if points:
        return points, f"год_регистрации={year}(+{points})"
    return 0, ""


def _calc_lessor_bonus(lessor_type: str) -> tuple[int, str]:
    """Бонус за тип лизингодателя (для хостингов — ПУТЬ Б)."""
    if not lessor_type:
        return 0, ""
    points = _LESSOR_TYPE_BONUSES.get(lessor_type, 0)
    if points:
        return points, f"лизингодатель={lessor_type}(+{points})"
    return 0, ""


def _calc_cross_path_bonus(company: dict) -> tuple[int, str]:
    """Бонус если компания найдена и как майнер (А) и как хостинг (Б)."""
    if company.get("cross_path"):
        return _CROSS_PATH_BONUS, f"перекрёст_путей(+{_CROSS_PATH_BONUS})"
    return 0, ""


# ════════════════════════════════════════════════════════════════════════════
# Основная функция скоринга
# ════════════════════════════════════════════════════════════════════════════

def score_company(company: dict[str, Any]) -> tuple[int, list[str]]:
    """
    Считает скоринговый балл 0–100 для компании.
    Возвращает (score, список_триггеров).
    """
    triggers: list[str] = []
    total: float = 0.0

    leasing_kw_score = float(company.get("leasing_keyword_score", 0.0) or 0.0)
    fixed_assets     = float(company.get("fixed_assets",          0.0) or 0.0)
    electricity      = float(company.get("electricity",           0.0) or 0.0)
    revenue          = float(company.get("revenue",               0.0) or 0.0)
    employees        = int(  company.get("employees",             0)   or 0)
    region           = str(  company.get("region",                "")  or "")
    reg_date         = str(  company.get("reg_date",              "")  or "")
    lessor_type      = str(  company.get("lessor_type",           "")  or "")

    # ════════════════════════════════════════════════════════════════════════
    # БЛОК 1 — Базовые веса
    # ════════════════════════════════════════════════════════════════════════

    # Лизинговые ключевые слова (40%)
    w = SCORE_WEIGHTS["leasing_keywords"] / 100.0
    part = leasing_kw_score * w * 100
    total += part
    if leasing_kw_score > 0:
        triggers.append(f"лизинг_ключевые={leasing_kw_score:.0%}(+{part:.0f})")

    # Основные средства (25%)
    w = SCORE_WEIGHTS["fixed_assets"] / 100.0
    part = _normalize(fixed_assets, _FIXED_ASSETS_MAX) * w * 100
    total += part
    if fixed_assets > 0:
        triggers.append(f"ОС={fixed_assets/1e6:.1f}млн(+{part:.0f})")

    bonus = _bonus(fixed_assets, _BONUS_FA)
    if bonus:
        total += bonus
        triggers.append(f"ОС_бонус(+{bonus})")

    # Электроэнергия (20%)
    w = SCORE_WEIGHTS["electricity"] / 100.0
    part = _normalize(electricity, _ELECTRICITY_MAX) * w * 100
    total += part
    if electricity > 0:
        triggers.append(f"ЭЭ={electricity/1e6:.1f}млн(+{part:.0f})")

    bonus = _bonus(electricity, _BONUS_EE)
    if bonus:
        total += bonus
        triggers.append(f"ЭЭ_бонус(+{bonus})")

    # Выручка (15%)
    w = SCORE_WEIGHTS["revenue"] / 100.0
    part = _normalize(revenue, _REVENUE_MAX) * w * 100
    total += part
    if revenue > 0:
        triggers.append(f"выручка={revenue/1e6:.1f}млн(+{part:.0f})")

    # Выручка на сотрудника
    if employees > 0 and revenue > 0:
        rev_per_emp = revenue / employees
        bonus = _bonus(rev_per_emp, _BONUS_REV_PER_EMP)
        if bonus:
            total += bonus
            triggers.append(f"выр/сотр={rev_per_emp/1e6:.1f}млн(+{bonus})")
    elif employees == 0 and revenue >= 5_000_000:
        total += 8
        triggers.append("0сотр+выручка(+8)")

    # ════════════════════════════════════════════════════════════════════════
    # БЛОК 2 — Новые бонусы (приоритизация, не жёсткий отсев)
    # ════════════════════════════════════════════════════════════════════════

    bonus, label = _calc_region_bonus(region)
    if bonus:
        total += bonus
        triggers.append(label)

    bonus, label = _calc_reg_year_bonus(reg_date)
    if bonus:
        total += bonus
        triggers.append(label)

    bonus, label = _calc_lessor_bonus(lessor_type)
    if bonus:
        total += bonus
        triggers.append(label)

    bonus, label = _calc_cross_path_bonus(company)
    if bonus:
        total += bonus
        triggers.append(label)

    # ════════════════════════════════════════════════════════════════════════
    # БЛОК 3 — Штрафы
    # ════════════════════════════════════════════════════════════════════════

    if not company.get("is_active", True):
        total *= 0.2
        triggers.append("ликвидирована(×0.2)")

    final = int(round(min(total, 100)))
    return final, triggers


# ════════════════════════════════════════════════════════════════════════════
# Приоритет и пакетный скоринг
# ════════════════════════════════════════════════════════════════════════════

def get_priority(score: int) -> str:
    if score >= 70:
        return "Горячий"
    elif score >= 40:
        return "Тёплый"
    return "Холодный"


def mark_cross_path(
    miner_inns: set[str],
    hosting_inns: set[str],
    companies: list[dict],
) -> None:
    """
    Помечает компании найденные одновременно в ПУТИ А (майнеры)
    и ПУТИ Б (хостинги). Вызывать ДО score_all().
    """
    cross = miner_inns & hosting_inns
    if cross:
        log.info(f"Перекрёстных компаний (майнер + хостинг): {len(cross)}")
    for company in companies:
        company["cross_path"] = company.get("inn") in cross


def score_all(
    companies: list[dict],
    miner_inns:   set[str] | None = None,
    hosting_inns: set[str] | None = None,
) -> list[dict]:
    """
    Рассчитывает скоринг для всех компаний, сортирует по убыванию score.

    Args:
        companies:    список компаний с обогащёнными данными
        miner_inns:   ИНН из ПУТИ А (лизингополучатели Федресурс)
        hosting_inns: ИНН из ПУТИ Б (лизингодатели Федресурс)
    """
    if miner_inns and hosting_inns:
        mark_cross_path(miner_inns, hosting_inns, companies)

    for company in companies:
        s, triggers = score_company(company)
        company["score"]          = s
        company["priority"]       = get_priority(s)
        company["score_triggers"] = " | ".join(triggers)

    companies.sort(key=lambda c: c["score"], reverse=True)

    hot  = sum(1 for c in companies if c["priority"] == "Горячий")
    warm = sum(1 for c in companies if c["priority"] == "Тёплый")
    cold = sum(1 for c in companies if c["priority"] == "Холодный")

    log.info(
        f"Скоринг завершён: всего={len(companies)} | "
        f"🔥 горячих={hot} | 🌡 тёплых={warm} | ❄️ холодных={cold}"
    )
    return companies
