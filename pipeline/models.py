"""
models.py — SQLAlchemy ORM + утилиты для работы с PostgreSQL
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Priority(str, enum.Enum):
    HOT  = "Горячий"
    WARM = "Тёплый"
    COLD = "Холодный"


class EnrichStatus(str, enum.Enum):
    PENDING  = "pending"
    DONE     = "done"
    FAILED   = "failed"


class Company(Base):
    """Основная таблица компаний."""
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inn: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)

    # Базовая информация (из ЕГРЮЛ)
    name: Mapped[str | None]    = mapped_column(Text)
    okvd_main: Mapped[str | None] = mapped_column(String(10))
    region: Mapped[str | None]  = mapped_column(String(100))
    status: Mapped[str | None]  = mapped_column(String(50))
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)

    # ── Финансы (из РусПрофайл / ГИРБО) ───────────────────────────────────
    revenue: Mapped[float]          = mapped_column(Float, default=0.0)
    cost_of_sales: Mapped[float]    = mapped_column(Float, default=0.0)
    fixed_assets: Mapped[float]     = mapped_column(Float, default=0.0)
    balance_total: Mapped[float]    = mapped_column(Float, default=0.0)
    net_profit: Mapped[float]       = mapped_column(Float, default=0.0)
    employees: Mapped[int]          = mapped_column(Integer, default=0)

    # ── Электроэнергия ──────────────────────────────────────────────────────
    electricity_expense: Mapped[float] = mapped_column(Float, default=0.0)

    # ── Федресурс / лизинг ─────────────────────────────────────────────────
    leasing_text: Mapped[str | None]  = mapped_column(Text)   # raw текст договоров
    leasing_keywords_found: Mapped[str | None] = mapped_column(Text)  # найденные слова (JSON)
    leasing_keyword_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1

    # ── ФНС ────────────────────────────────────────────────────────────────
    tax_burden: Mapped[float | None]  = mapped_column(Float)   # % налог.нагрузки
    avg_employees_fns: Mapped[int | None] = mapped_column(Integer)

    # ── Скоринг ────────────────────────────────────────────────────────────
    score: Mapped[int]            = mapped_column(Integer, default=0)
    priority: Mapped[Priority | None] = mapped_column(Enum(Priority))
    score_triggers: Mapped[str | None] = mapped_column(Text)   # JSON список

    # ── Состояние обогащения ────────────────────────────────────────────────
    enrich_rusprofile: Mapped[EnrichStatus] = mapped_column(
        Enum(EnrichStatus), default=EnrichStatus.PENDING
    )
    enrich_fedresurs: Mapped[EnrichStatus] = mapped_column(
        Enum(EnrichStatus), default=EnrichStatus.PENDING
    )
    enrich_fns: Mapped[EnrichStatus] = mapped_column(
        Enum(EnrichStatus), default=EnrichStatus.PENDING
    )

    # ── Мета ────────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    report_year: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<Company inn={self.inn} score={self.score} priority={self.priority}>"


# ── Утилиты БД ──────────────────────────────────────────────────────────────

async def get_engine(database_url: str):
    """Создаёт async engine. Вызывается один раз при старте."""
    from sqlalchemy.ext.asyncio import create_async_engine
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


async def init_db(engine) -> None:
    """Создаёт таблицы если не существуют."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session(engine):
    """Возвращает фабрику async context manager для сессий."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    return _session
