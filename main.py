"""
main.py — точка входа, оркестрирует все этапы пайплайна

Режимы запуска (--mode):
    egrul      — сбор ИНН из ЕГРЮЛ по ОКВЭД → обогащение → скоринг
    fedresurs  — поиск по ключевым словам на Федресурсе →
                 ПУТЬ А (майнеры/лизингополучатели) +
                 ПУТЬ Б (хостинги/лизингодатели) → обогащение → скоринг
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
from pipeline.fedresurs import search_by_keywords, MINING_SEARCH_KEYWORDS
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
            "Все_ОКВЭДы":        json.dumps(c.get("okveds_all", []), ensure_ascii=False),
            "Руководитель":      c.get("director_name", ""),
            "Дата_регистрации":  c.get("reg_date", ""),
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
            "Тип_лизингодателя": c.get("lessor_type", ""),
            "Перекрёст_путей":   "да" if c.get("cross_path") else "нет",
            "В_реестре_майнеров": "да" if c.get("in_miner_registry") else "нет",
            "В_реестре_инфраструктуры": "да" if c.get("in_infra_registry") else "нет",
            "Путь":              c.get("pipeline_path", ""),
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


async def run_fedresurs_pipeline(
    keywords: list[str] | None = None,
    output: str | None = None,
    skip_db: bool = False,
) -> pd.DataFrame | None:
    """
    Пайплайн на основе Федресурс:
      1. Поиск по ключевым словам → мiner_inns (А) + hosting_inns (Б)
      2. Обогащение всех ИНН (ФНС / ГИРБО / ЕГРЮЛ)
      3. Фильтрация → Скоринг с перекрёстным бонусом
      4. Сохранение CSV
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y-%m-%d")

    # ── Этап 1: Федресурс keyword-search ────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 1: Федресурс — поиск по ключевым словам")
    log.info("=" * 60)

    fed_result = await search_by_keywords(keywords or MINING_SEARCH_KEYWORDS)

    if fed_result.get("error"):
        log.error(f"Федресурс недоступен: {fed_result['error']}")
        return None

    miner_inns_map   = fed_result["miner_inns"]    # {inn: [тексты]}
    hosting_inns_map = fed_result["hosting_inns"]  # {inn: {name, okved, lessor_type}}
    stats = fed_result["stats"]

    log.info(
        f"Федресурс итог: майнеров={len(miner_inns_map)} | "
        f"хостингов={len(hosting_inns_map)} | "
        f"финансовых отсеяно={stats['lessors_financial']}"
    )

    # ── Этап 2: Формируем единый список ИНН ─────────────────────────────────
    all_inns = list(dict.fromkeys(
        list(miner_inns_map.keys()) + list(hosting_inns_map.keys())
    ))[:MAX_INNS_PER_RUN]

    if not all_inns:
        log.error("Нет ИНН для обработки.")
        return None

    log.info(f"Уникальных ИНН для обогащения: {len(all_inns)}")

    # Сохраняем разделение на пути для дальнейшей разметки
    miner_inns_set   = set(miner_inns_map.keys())
    hosting_inns_set = set(hosting_inns_map.keys())

    # ── Этап 3: Обогащение ──────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 3: Обогащение (ФНС / ГИРБО / ЕГРЮЛ)")
    log.info("=" * 60)

    enriched = await enrich_batch(all_inns)

    # Проставляем leasing_texts и lessor_type из Федресурс
    for company in enriched:
        inn = company.get("inn", "")

        # Тексты договоров (для скоринга ключевых слов)
        if inn in miner_inns_map:
            company.setdefault("leasing_texts", []).extend(miner_inns_map[inn])

        # Тип лизингодателя + тексты из договоров (для хостингов — Путь Б)
        if inn in hosting_inns_map:
            company["lessor_type"] = hosting_inns_map[inn].get("lessor_type", "")
            company.setdefault("leasing_texts", []).extend(
                hosting_inns_map[inn].get("texts", [])
            )

        # Пометка пути
        paths = []
        if inn in miner_inns_set:
            paths.append("А(майнер)")
        if inn in hosting_inns_set:
            paths.append("Б(хостинг)")
        company["pipeline_path"] = "+".join(paths)

    log.info(f"Обогащено: {len(enriched)} компаний")

    # ── Этап 4: Фильтрация ──────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 4: Каскадная фильтрация")
    log.info("=" * 60)

    passed, filter_stats = apply_cascade_filters(enriched)
    log.info(f"После фильтрации: {len(passed)} из {len(enriched)} | {filter_stats}")

    if not passed:
        log.warning("Нет компаний после фильтрации.")
        return None

    # ── Этап 5: Скоринг с перекрёстным бонусом ──────────────────────────────
    log.info("=" * 60)
    log.info("ЭТАП 5: Скоринг")
    log.info("=" * 60)

    scored = score_all(
        passed,
        miner_inns=miner_inns_set,
        hosting_inns=hosting_inns_set,
    )

    # ── Сохранение ───────────────────────────────────────────────────────────
    if not skip_db:
        await _save_to_db(scored)

    df_data = []
    for c in scored:
        df_data.append({
            "ИНН":               c.get("inn", ""),
            "Компания":          c.get("name", ""),
            "Регион":            c.get("region", ""),
            "ОКВЭД":             c.get("okvd_main", ""),
            "Все_ОКВЭДы":        json.dumps(c.get("okveds_all", []), ensure_ascii=False),
            "Руководитель":      c.get("director_name", ""),
            "Дата_регистрации":  c.get("reg_date", ""),
            "Выручка_руб":       int(c.get("revenue", 0) or 0),
            "ОС_руб":            int(c.get("fixed_assets", 0) or 0),
            "Расходы_ЭЭ_руб":   int(c.get("electricity", 0) or 0),
            "Сотрудники":        int(c.get("employees", 0) or 0),
            "Лизинг_ключ_слова": c.get("leasing_keywords_found", "[]"),
            "Тип_лизингодателя": c.get("lessor_type", ""),
            "Перекрёст_путей":   "да" if c.get("cross_path") else "нет",
            "В_реестре_майнеров": "да" if c.get("in_miner_registry") else "нет",
            "Путь":              c.get("pipeline_path", ""),
            "Скоринг":           c.get("score", 0),
            "Приоритет":         c.get("priority", ""),
            "Триггеры":          c.get("score_triggers", ""),
        })

    df = pd.DataFrame(df_data)
    out_path = output or str(OUTPUT_DIR / f"leads_fedresurs_{today}.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    hot  = (df["Приоритет"] == "Горячий").sum()
    warm = (df["Приоритет"] == "Тёплый").sum()

    print("\n" + "=" * 60)
    print("  ГОТОВО — ФЕДРЕСУРС ПАЙПЛАЙН")
    print(f"  Компаний в результате: {len(df)}")
    print(f"  Горячих:  {hot}")
    print(f"  Тёплых:   {warm}")
    print(f"  CSV:      {out_path}")
    print("=" * 60 + "\n")

    return df


def _parse_args():
    p = argparse.ArgumentParser(description="Mining/Hosting lead parser v2")
    p.add_argument(
        "--mode",
        choices=["egrul", "fedresurs"],
        default="fedresurs",
        help=(
            "egrul — сбор ИНН из ЕГРЮЛ по ОКВЭД; "
            "fedresurs — поиск по ключевым словам на Федресурсе (рекомендуется)"
        ),
    )
    p.add_argument("--category", choices=["hosting", "mining"], default="hosting",
        help="Только для режима egrul")
    p.add_argument("--source", default="api",
        help='Только для режима egrul: "api" или путь к файлу inns.txt / inns.csv')
    p.add_argument("--output", default=None, help="Путь к выходному CSV")
    p.add_argument("--skip-db", action="store_true", help="Не писать в PostgreSQL")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.mode == "fedresurs":
        asyncio.run(run_fedresurs_pipeline(
            output=args.output,
            skip_db=args.skip_db,
        ))
    else:
        asyncio.run(run_full_pipeline(
            category=args.category,
            inn_source=args.source,
            output=args.output,
            skip_db=args.skip_db,
        ))
