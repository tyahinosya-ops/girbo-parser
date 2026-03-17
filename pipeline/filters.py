"""
filters.py — Этап 3: каскадная фильтрация
  1. Выручка
  2. Баланс ОС
  3. Расходы на ЭЭ
  4. NLP (rapidfuzz) по тексту лизинга из Федресурса
"""
from __future__ import annotations

import json
import logging
from typing import Any

from config import (
    MIN_REVENUE, MIN_FIXED_ASSETS, MIN_ELECTRICITY_EXPENSE,
    MINING_KEYWORDS, FUZZY_THRESHOLD,
)

log = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, process as rfuzz_process
    _RAPIDFUZZ_OK = True
except ImportError:
    log.warning("rapidfuzz не установлен — NLP фильтр будет использовать простое вхождение строки")
    _RAPIDFUZZ_OK = False


# ── NLP по тексту лизинга ──────────────────────────────────────────────────

def _fuzzy_match_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    Ищет ключевые слова в тексте с помощью rapidfuzz (partial ratio).
    Возвращает список найденных ключевых слов.
    """
    if not text:
        return []

    text_lower = text.lower()
    found: list[str] = []

    if _RAPIDFUZZ_OK:
        for kw in keywords:
            score = fuzz.partial_ratio(kw.lower(), text_lower)
            if score >= FUZZY_THRESHOLD:
                found.append(kw)
    else:
        # Фоллбэк: простое вхождение
        for kw in keywords:
            if kw.lower() in text_lower:
                found.append(kw)

    return found


def compute_leasing_score(leasing_texts: list[str]) -> tuple[float, list[str]]:
    """
    Анализирует тексты лизинговых договоров.
    Возвращает:
      - score 0..1 (доля ключевых слов от максимума)
      - список найденных слов
    """
    if not leasing_texts:
        return 0.0, []

    all_found: set[str] = set()
    combined = " ".join(leasing_texts).lower()

    found = _fuzzy_match_keywords(combined, MINING_KEYWORDS)
    all_found.update(found)

    score = len(all_found) / len(MINING_KEYWORDS) if MINING_KEYWORDS else 0.0
    return min(score, 1.0), sorted(all_found)


# ── Каскадные фильтры ──────────────────────────────────────────────────────

def _passes_revenue(company: dict) -> tuple[bool, str]:
    rev = company.get("revenue", 0.0) or 0.0
    if rev < MIN_REVENUE:
        return False, f"выручка {rev:,.0f} < {MIN_REVENUE:,.0f}"
    return True, ""


def _passes_fixed_assets(company: dict) -> tuple[bool, str]:
    fa = company.get("fixed_assets", 0.0) or 0.0
    if fa < MIN_FIXED_ASSETS:
        return False, f"ОС {fa:,.0f} < {MIN_FIXED_ASSETS:,.0f}"
    return True, ""


def _passes_electricity(company: dict) -> tuple[bool, str]:
    ee = company.get("electricity", 0.0) or 0.0
    if ee == 0.0:
        # Данные отсутствуют (ГИРБО недоступен) — не отфильтровываем,
        # скоринг учтёт отсутствие данных через нулевой вес.
        return True, ""
    if ee < MIN_ELECTRICITY_EXPENSE:
        return False, f"ЭЭ {ee:,.0f} < {MIN_ELECTRICITY_EXPENSE:,.0f}"
    return True, ""


def _passes_leasing_nlp(company: dict) -> tuple[bool, str]:
    """Проходит если найдено хоть одно ключевое слово в лизинге."""
    texts = company.get("leasing_texts", [])
    if not texts:
        # Нет данных о лизинге — не отфильтровываем, просто score=0
        return True, ""
    _, found = compute_leasing_score(texts)
    if not found:
        return False, "нет ключевых слов в лизинге"
    return True, ""


# Порядок фильтров: от дешёвого к дорогому
_CASCADE: list[tuple[str, Any]] = [
    ("revenue",       _passes_revenue),
    ("fixed_assets",  _passes_fixed_assets),
    ("electricity",   _passes_electricity),
    ("leasing_nlp",   _passes_leasing_nlp),
]


def apply_cascade_filters(
    companies: list[dict],
    skip_inactive: bool = True,
) -> tuple[list[dict], dict[str, int]]:
    """
    Применяет каскадные фильтры.
    Возвращает (прошедшие, статистика по причинам отсева).
    """
    passed: list[dict] = []
    stats: dict[str, int] = {name: 0 for name, _ in _CASCADE}
    stats["inactive"] = 0

    for company in companies:
        # Предварительно: ликвидированные
        if skip_inactive and not company.get("is_active", True):
            stats["inactive"] += 1
            continue

        rejected = False
        for filter_name, filter_fn in _CASCADE:
            ok, reason = filter_fn(company)
            if not ok:
                log.debug(f"ИНН {company.get('inn')} отсеян [{filter_name}]: {reason}")
                stats[filter_name] += 1
                rejected = True
                break

        if not rejected:
            # Вычисляем leasing_score и добавляем в запись
            leasing_score, found_kw = compute_leasing_score(
                company.get("leasing_texts", [])
            )
            company["leasing_keyword_score"]   = leasing_score
            company["leasing_keywords_found"]  = json.dumps(found_kw, ensure_ascii=False)
            passed.append(company)

    log.info(
        f"Каскадная фильтрация: "
        f"всего={len(companies)}, прошло={len(passed)}, "
        f"отсев={stats}"
    )
    return passed, stats
