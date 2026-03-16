"""
main.py — точка входа, оркестрирует все четыре этапа пайплайна
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from config import (
    DATABASE_URL, TARGET_OKVEDS, TARGET_REGIONS,
    MAX_INNS_PER_RUN, PROXIES, REPORT_YEAR,
)
from pipeline.collector import collect_inns_egrul
from pipeline.enrichment import enrich_batch
from pipeline.filters import apply_cascade_filters
from pipeline.scoring import score_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")


def _load_inns_from_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    if path.endswith(".csv"):
        df  = pd.read_csv(path, dtype=str)
        col = next(
            (c for c in df.columns if "инн" in c.lower() or "inn" in c.lower()),
            df.columns[0],
        )
        inns = df[col].str.strip().tolist()
    else:
        with open(path, encoding="utf-8") as f:
            inns = [line.strip() for line in f if line.strip()]
    return [i for i in inns if i.isdigit() and len(i) in (10, 12)]


async def _save_to_db(companies: list[dict]) -> None:
    """Сохраняет/обновляет записи в PostgreSQL."""
    try:
        from pipeline.models import get_engine, init_db, get_session, Company, Priority, EnrichStatus
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        engine = await get_engine(DATABASE_URL)
        await init_db(engine)
        session_ctx = get_session(engine)

        async with session_ctx() as session:
            for c in companies:
                stmt = pg_insert(Company).values(
                    inn=c["inn"],
                    name=c.get("name", ""),
                    okvd_main=c.get("okvd_main", ""),
                    region=c.get("region", ""),
                    is_active=c.get("is_active", True),
                    status=c.get("status", ""),
                    revenue=c.get("revenue", 0.0),
                    cost_of_sales=c.get("cost_of_sales", 0.0),
                    fixed_assets=c.get("fixed_assets", 0.0),
                    balance_total=c.get("balance_total", 0.0),
                    net_profit=c.get("net_profit", 0.0),
                    employees=c.get("employees", 0),
                    electricity_expense=c.get("electricity", 0.0),
                    leasing_text="\n---\n".join(c.get("leasing_texts", [])),
                    leasing_keywords_found=c.get("leasing_keywords_found", "[]"),
                    leasing_keyword_score=c.get("leasing_keyword_score", 0.0),
                    score=c.get("score", 0),
                    priority=c.get("priority"),
                    score_triggers=c.get("score_triggers", ""),
                    enrich_rusprofile=EnrichStatus.DONE,
                    enrich_fedresurs=EnrichStatus.DONE,
                    enrich_fns=EnrichStatus.DONE,
                    report_year=REPORT_YEAR,
                ).on_conflict_do_update(
                    index_elements=["inn"],
                    set_={
                        "revenue":             c.get("revenue", 0.0),
                        "fixed_assets":        c.get("fixed_assets", 0.0),
                        "balance_total":       c.get("balance_total", 0.0),
                        "electricity_expense": c.get("electricity", 0.0),
                        "leasing_keyword_score": c.get("leasing_keyword_score", 0.0),
                        "score":               c.get("score", 0),
                        "priority":            c.get("priority"),
                        "score_triggers":      c.get("score_triggers", ""),
                    }
                )
                await session.execute(stmt)
            await session.commit()

        log.info(f"БД: сохранено {len(companies)} записей")
        await engine.dispose()

    except Exception as e:
        log.warning(f"Ошибка записи в БД: {e} (продолжаем без БД)")


async def run_full_pipeline(
    category: str = "hosting",
    inn_source: str = "api",
    okveds: list[str] | None = None,
    regions: list[str] | None = None,
    output: str | None = None,
    skip_db: bool = False,
) -> pd.DataFrame | None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    today  = date.today().strftime("%Y-%m-%d")
    suffix = category

    # ── Этап 1: Сбор ИНН ──────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"ЭТАП 1: Сбор ИНН (категория={category}, источник={inn_source})")
    log.info("=" * 60)

    if inn_source != "api":
        inns = _load_inns_from_file(inn_source)
        log.info(f"Загружено {len(inns)} ИНН из файла")
    else:
        inns = await collect_inns_egrul(
            okveds=okveds or TARGET_OKVEDS,
            regions=regions or TARGET_REGIONS,
            proxies=PROXIES if PROXIES else None,
        )

    if not inns:
        log.error("ИНН не получены — завершаем.")
        return None

    # Дедупликация и ограничение
    inns = list(dict.fromkeys(inns))[:MAX_INNS_PER_RUN]
    log.info(f"Уникальных ИНН для обработки: {len(inns)}")

    inn_file = OUTPUT_DIR / f"inns_{suffix}_{today}.txt"
    inn_file.write_text("\n".join(inns), encoding="utf-8")
    log.info(f"ИНН сохранены: {inn_file}")

    # ── Этап 2: Параллельное обогащение ────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 2: Параллельное обогащение (RusProfile + Федресурс + ФНС)")
    log.info("=" * 60)

    enriched = await enrich_batch(inns)
    log.info(f"Обогащено: {len(enriched)} компаний")

    # ── Этап 3: Каскадная фильтрация ───────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 3: Каскадная фильтрация")
    log.info("=" * 60)

    passed, filter_stats = apply_cascade_filters(enriched)
    log.info(f"После фильтрации: {len(passed)} из {len(enriched)}")
    log.info(f"Статистика отсева: {filter_stats}")

    if not passed:
        log.warning("Нет компаний после фильтрации.")
        return None

    # ── Этап 4: Скоринг ────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 4: Взвешенный скоринг")
    log.info("=" * 60)

    scored = score_all(passed)

    # ── Сохранение ─────────────────────────────────────────────────────────
    if not skip_db:
        await _save_to_db(scored)

    # CSV
    df_data = []
    for c in scored:
        df_data.append({
            "ИНН":               c.get("inn", ""),
            "Компания":          c.get("name", ""),
            "Регион":            c.get("region", ""),
            "ОКВЭД":             c.get("okvd_main", ""),
            "Выручка_руб":       int(c.get("revenue", 0) or 0),
            "Себестоимость_руб": int(c.get("cost_of_sales", 0) or 0),
            "ОС_руб":            int(c.get("fixed_assets", 0) or 0),
            "Баланс_руб":        int(c.get("balance_total", 0) or 0),
            "ЧистПриб_руб":      int(c.get("net_profit", 0) or 0),
            "Расходы_ЭЭ_руб":   int(c.get("electricity", 0) or 0),
            "Сотрудники":        int(c.get("employees", 0) or 0),
            "Лизинг_кол_сообщ": c.get("leasing_raw_count", 0),
            "Лизинг_ключ_слова": c.get("leasing_keywords_found", "[]"),
            "Лизинг_score":      round(c.get("leasing_keyword_score", 0.0), 3),
            "Скоринг":           c.get("score", 0),
            "Приоритет":         c.get("priority", ""),
            "Триггеры":          c.get("score_triggers", ""),
        })

    df = pd.DataFrame(df_data)
    out_path = output or str(OUTPUT_DIR / f"leads_{suffix}_{today}.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    hot  = (df["Приоритет"] == "Горячий").sum()
    warm = (df["Приоритет"] == "Тёплый").sum()

    print("\n" + "=" * 60)
    print(f"  ГОТОВО — {category.upper()}")
    print(f"  Компаний в результате: {len(df)}")
    print(f"  Горячих:  {hot}")
    print(f"  Тёплых:   {warm}")
    print(f"  CSV:      {out_path}")
    print("=" * 60 + "\n")

    return df


def _parse_args():
    p = argparse.ArgumentParser(description="Mining/Hosting lead parser v2")
    p.add_argument("--category", choices=["hosting", "mining"], default="hosting")
    p.add_argument("--source", default="api",
        help='"api" — ЕГРЮЛ, или путь к файлу inns.txt / inns.csv')
    p.add_argument("--output", default=None, help="Путь к выходному CSV")
    p.add_argument("--skip-db", action="store_true", help="Не писать в PostgreSQL")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    asyncio.run(run_full_pipeline(
        category=args.category,
        inn_source=args.source,
        output=args.output,
        skip_db=args.skip_db,
    ))
