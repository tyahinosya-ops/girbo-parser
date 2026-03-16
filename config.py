"""
config.py — централизованная конфигурация пайплайна
"""
import os
from dataclasses import dataclass, field

# ── PostgreSQL ──────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/mining_parser",
)

# ── Redis / Celery ──────────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER: str = REDIS_URL
CELERY_BACKEND: str = REDIS_URL

# ── Прокси ─────────────────────────────────────────────────────────────────
# Список прокси вида "http://user:pass@host:port"
# Оставь пустым если без прокси (рискованно для продакшена)
PROXIES: list[str] = [p for p in os.getenv("PROXIES", "").split(",") if p.strip()]

# ── ОКВЭД для сбора ────────────────────────────────────────────────────────
TARGET_OKVEDS: list[str] = [
    "26.20",  # производство ЭВМ и периферии
    "35.11",  # производство электроэнергии
    "35.14",  # торговля электроэнергией
    "63.11",  # обработка данных / ЦОД
    "62.09",  # прочая ИТ-деятельность
    "62.01",  # разработка ПО
    "46.51",  # оптовая торговля компьютерами
    "46.52",  # оптовая торговля электроникой
    "06.10",  # добыча нефти (часто используют как прикрытие)
]

# ── Регионы (коды для ЕГРЮЛ) ────────────────────────────────────────────────
TARGET_REGIONS: list[str] = [
    "38",  # Иркутская область
    "24",  # Красноярский край
    "19",  # Республика Хакасия
    "03",  # Республика Бурятия
    "10",  # Республика Карелия
    "07",  # Кабардино-Балкария
    "05",  # Дагестан
]

# ── Фильтры (каскад) ───────────────────────────────────────────────────────
MIN_REVENUE: float = 1_000_000          # минимальная выручка, руб
MIN_FIXED_ASSETS: float = 500_000       # минимальный баланс ОС, руб
MIN_ELECTRICITY_EXPENSE: float = 500_000  # мин расходы на ЭЭ, руб

# ── Майнинговые ключевые слова (rapidfuzz) ─────────────────────────────────
MINING_KEYWORDS: list[str] = [
    "асик", "майнер", "antminer", "whatsminer",
    "эвм", "сервер", "антмайнер", "ватсмайнер",
    "bitcoin mining", "добыча криптовалюты",
    "gpu ферма", "bitmain", "microbt",
    "криптовалюта лизинг", "майнинг оборудование",
]
FUZZY_THRESHOLD: int = 85  # порог схожести rapidfuzz

# ── Скоринг (веса в %) ─────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "leasing_keywords": 40,
    "fixed_assets":     25,
    "electricity":      20,
    "revenue":          15,
}

# ── Запросы ────────────────────────────────────────────────────────────────
REPORT_YEAR: int = 2023
REQUEST_DELAY_MIN: float = 2.0
REQUEST_DELAY_MAX: float = 8.0
MAX_INNS_PER_RUN: int = 1000
MAX_RETRIES: int = 3
ENRICHMENT_CONCURRENCY: int = 5  # сколько ИНН обогащаем параллельно

# ── Playwright ──────────────────────────────────────────────────────────────
PLAYWRIGHT_HEADLESS: bool = True
PLAYWRIGHT_TIMEOUT_MS: int = 30_000

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]
