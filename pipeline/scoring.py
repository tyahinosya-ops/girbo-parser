"""
scoring.py — Этап 4: взвешенный скоринг компаний
  Веса:
    leasing_keywords  40%
    fixed_assets      25%
    electricity       20%
    revenue           15%
"""
from __future__ import annotations

import logging
from typing import Any

from config import SCORE_WEIGHTS

log = logging.getLogger(__name__)

# Пороги для нормализации (всё выше максимума = 1.0)
_REVENUE_MAX        = 1_000_000_000   # 1 млрд
_FIXED_ASSETS_MAX   = 500_000_000     # 500 млн
_ELECTRICITY_MAX    = 100_000_000     # 100 млн

# Бонусы за абсолютные пороги (дополнительные очки поверх веса)
_BONUS_EE = [
    (50_000_000,  15),
    (20_000_000,  10),
    (10_000_000,   5),
]
_BONUS_FA = [
    (200_000_000, 10),
    (50_000_000,   5),
]
_BONUS_REV_PER_EMP = [
    (20_000_000,  10),
    (10_000_000,   5),
]


def _normalize(value: float, max_value: float) -> float:
    """Линейная нормализация 0..1."""
    if max_value <= 0 or value <= 0:
        return 0.0
    return min(value / max_value, 1.0)


def _bonus(value: float, thresholds: list[tuple[float, int]]) -> int:
    for threshold, points in thresholds:
        if value >= threshold:
            return points
    return 0


def score_company(company: dict[str, Any]) -> tuple[int, list[str]]:
    """
    Считает скоринговый балл 0–100 для компании.
    Возвращает (score, список_триггеров).
    """
    triggers: list[str] = []
    total_score = 0.0

    leasing_kw_score = float(company.get("leasing_keyword_score", 0.0) or 0.0)
    fixed_assets     = float(company.get("fixed_assets", 0.0) or 0.0)
    electricity      = float(company.get("electricity", 0.0) or 0.0)
    revenue          = float(company.get("revenue", 0.0) or 0.0)
    employees        = int(company.get("employees", 0) or 0)

    # ── 1. Лизинговые ключевые слова (40%) ──────────────────────────────
    w_leasing = SCORE_WEIGHTS["leasing_keywords"] / 100.0
    leasing_part = leasing_kw_score * w_leasing * 100
    total_score += leasing_part
    if leasing_kw_score > 0:
        kw_found = company.get("leasing_keywords_found", "[]")
        triggers.append(f"лизинг_ключевые={leasing_kw_score:.0%}(+{leasing_part:.0f})")

    # ── 2. Основные средства (25%) ───────────────────────────────────────
    w_fa   = SCORE_WEIGHTS["fixed_assets"] / 100.0
    fa_norm = _normalize(fixed_assets, _FIXED_ASSETS_MAX)
    fa_part = fa_norm * w_fa * 100
    total_score += fa_part
    if fixed_assets > 0:
        triggers.append(f"ОС={fixed_assets/1e6:.1f}млн(+{fa_part:.0f})")

    fa_bonus = _bonus(fixed_assets, _BONUS_FA)
    if fa_bonus:
        total_score += fa_bonus
        triggers.append(f"ОС_бонус(+{fa_bonus})")

    # ── 3. Расходы на электроэнергию (20%) ───────────────────────────────
    w_ee   = SCORE_WEIGHTS["electricity"] / 100.0
    ee_norm = _normalize(electricity, _ELECTRICITY_MAX)
    ee_part = ee_norm * w_ee * 100
    total_score += ee_part
    if electricity > 0:
        triggers.append(f"ЭЭ={electricity/1e6:.1f}млн(+{ee_part:.0f})")

    ee_bonus = _bonus(electricity, _BONUS_EE)
    if ee_bonus:
        total_score += ee_bonus
        triggers.append(f"ЭЭ_бонус(+{ee_bonus})")

    # ── 4. Выручка (15%) ─────────────────────────────────────────────────
    w_rev   = SCORE_WEIGHTS["revenue"] / 100.0
    rev_norm = _normalize(revenue, _REVENUE_MAX)
    rev_part = rev_norm * w_rev * 100
    total_score += rev_part
    if revenue > 0:
        triggers.append(f"выручка={revenue/1e6:.1f}млн(+{rev_part:.0f})")

    # ── Бонус: выручка на сотрудника ─────────────────────────────────────
    if employees > 0 and revenue > 0:
        rev_per_emp = revenue / employees
        rep_bonus = _bonus(rev_per_emp, _BONUS_REV_PER_EMP)
        if rep_bonus:
            total_score += rep_bonus
            triggers.append(f"выр/сотр={rev_per_emp/1e6:.1f}млн(+{rep_bonus})")
    elif employees == 0 and revenue >= 5_000_000:
        total_score += 8
        triggers.append("0сотр+выручка(+8)")

    # ── Штраф: ликвидирована ─────────────────────────────────────────────
    if not company.get("is_active", True):
        total_score *= 0.2
        triggers.append("ликвидирована(×0.2)")

    final = int(round(min(total_score, 100)))
    return final, triggers


def get_priority(score: int) -> str:
    if score >= 70:
        return "Горячий"
    elif score >= 40:
        return "Тёплый"
    return "Холодный"


def score_all(companies: list[dict]) -> list[dict]:
    """
    Рассчитывает скоринг для всех компаний,
    сортирует по убыванию score.
    """
    for company in companies:
        s, triggers = score_company(company)
        company["score"]          = s
        company["priority"]       = get_priority(s)
        company["score_triggers"] = " | ".join(triggers)

    companies.sort(key=lambda c: c["score"], reverse=True)
    log.info(
        f"Скоринг: {len(companies)} компаний | "
        f"горячих={sum(1 for c in companies if c['priority']=='Горячий')} | "
        f"тёплых={sum(1 for c in companies if c['priority']=='Тёплый')}"
    )
    return companies
