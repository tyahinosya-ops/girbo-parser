"""
test_pipeline.py — локальный smoke-test без сети
  Подменяет внешние вызовы моковыми данными и прогоняет
  фильтрацию + скоринг с реальными числами.
"""
import asyncio
import json
import sys
sys.path.insert(0, "/home/user/girbo-parser")

from pipeline.filters import apply_cascade_filters, compute_leasing_score
from pipeline.scoring import score_all, get_priority

# ── Мок-данные (имитируют вывод enrich_batch) ──────────────────────────────
MOCK_COMPANIES = [
    {
        "inn": "3811000001",
        "name": "ООО СИБИРЬ ДАТА ЦЕНТР",
        "region": "Иркутская область",
        "okvd_main": "63.11",
        "is_active": True,
        "revenue":        480_000_000,
        "cost_of_sales":  310_000_000,
        "fixed_assets":   290_000_000,
        "balance_total":  420_000_000,
        "net_profit":      55_000_000,
        "electricity":     72_000_000,
        "employees": 18,
        "leasing_texts": [
            "Уведомление о финансовой аренде (лизинге). "
            "Предмет лизинга: ASIC-майнеры AntMiner S19 Pro (200 шт.), "
            "серверное оборудование для ЦОД, эвм специализированные. "
            "Лизингодатель: ООО ЛК ТЕХНО. Должник: ООО СИБИРЬ ДАТА ЦЕНТР.",
            "Доп. соглашение: Whatsminer M50 (50 шт.), GPU-фермы для добычи криптовалюты.",
        ],
        "leasing_raw_count": 2,
    },
    {
        "inn": "2465000002",
        "name": "АО КРАСНОЯРСК ЭНЕРГО",
        "region": "Красноярский край",
        "okvd_main": "35.11",
        "is_active": True,
        "revenue":        920_000_000,
        "cost_of_sales":  600_000_000,
        "fixed_assets":   750_000_000,
        "balance_total": 1_100_000_000,
        "net_profit":     110_000_000,
        "electricity":      8_000_000,  # мало ЭЭ в отчётности
        "employees": 340,
        "leasing_texts": [
            "Лизинг трансформаторного оборудования, подстанции 110кВ.",
        ],
        "leasing_raw_count": 1,
    },
    {
        "inn": "1901000003",
        "name": "ООО ХАКАСИЯ МАЙНИНГ",
        "region": "Республика Хакасия",
        "okvd_main": "26.20",
        "is_active": True,
        "revenue":         95_000_000,
        "cost_of_sales":   60_000_000,
        "fixed_assets":    82_000_000,
        "balance_total":  120_000_000,
        "net_profit":      18_000_000,
        "electricity":     38_000_000,
        "employees": 4,
        "leasing_texts": [
            "Предмет лизинга: антмайнер S17, whatsminer M30S++, "
            "серверы для майнинг-фермы. Добыча криптовалюты Bitcoin.",
            "GPU ферма на 500 карт, ASIC bitmain серия 19.",
        ],
        "leasing_raw_count": 2,
    },
    {
        "inn": "7700000004",
        "name": "ООО РОМАШКА ТРЕЙД",
        "region": "Москва",
        "okvd_main": "46.51",
        "is_active": True,
        "revenue":    500_000,  # < 1 млн → отсев на 1-м фильтре
        "cost_of_sales": 400_000,
        "fixed_assets":  50_000,
        "balance_total": 200_000,
        "net_profit":    10_000,
        "electricity":        0,
        "employees": 2,
        "leasing_texts": [],
        "leasing_raw_count": 0,
    },
    {
        "inn": "3800000005",
        "name": "ООО ИТ-СЕРВИС ИРКУТСК",
        "region": "Иркутская область",
        "okvd_main": "62.09",
        "is_active": True,
        "revenue":      12_000_000,
        "cost_of_sales": 8_000_000,
        "fixed_assets":    400_000,  # < 500 тыс → отсев на 2-м фильтре
        "balance_total":   900_000,
        "net_profit":      500_000,
        "electricity":     200_000,
        "employees": 5,
        "leasing_texts": [],
        "leasing_raw_count": 0,
    },
    {
        "inn": "1000000006",
        "name": "ООО БУРЯАД ДАТА",
        "region": "Республика Бурятия",
        "okvd_main": "63.11",
        "is_active": False,   # ликвидирована
        "revenue":      50_000_000,
        "cost_of_sales": 30_000_000,
        "fixed_assets":  25_000_000,
        "balance_total": 40_000_000,
        "net_profit":     8_000_000,
        "electricity":   15_000_000,
        "employees": 10,
        "leasing_texts": ["asic майнер серверы bitcoin"],
        "leasing_raw_count": 1,
    },
]


async def main():
    print("\n" + "=" * 60)
    print("  SMOKE TEST — полный пайплайн без сети")
    print(f"  Входных компаний: {len(MOCK_COMPANIES)}")
    print("=" * 60)

    # ── Этап 3: Каскадная фильтрация ────────────────────────────────
    print("\n[ЭТАП 3] Каскадная фильтрация")
    passed, stats = apply_cascade_filters(MOCK_COMPANIES, skip_inactive=True)
    print(f"  Прошли фильтр: {len(passed)}/{len(MOCK_COMPANIES)}")
    print(f"  Отсев: {stats}")

    # ── Этап 4: Скоринг ─────────────────────────────────────────────
    print("\n[ЭТАП 4] Взвешенный скоринг")
    scored = score_all(passed)

    print("\n{'─'*58}")
    print(f"{'ИНН':<14} {'Компания':<28} {'Скор':>4} {'Приоритет'}")
    print("─" * 58)
    for c in scored:
        name = c.get("name", "")[:26]
        print(f"{c['inn']:<14} {name:<28} {c['score']:>4}  {c['priority']}")
        print(f"  Триггеры: {c['score_triggers']}")

    print("\n[ДЕТАЛИ ЛИЗИНГА]")
    for c in scored:
        score, found = compute_leasing_score(c.get("leasing_texts", []))
        if found:
            print(f"  {c['inn']} → {found} (score={score:.0%})")

    # ── Сохраняем CSV ────────────────────────────────────────────────
    import pandas as pd
    from pathlib import Path
    Path("output").mkdir(exist_ok=True)
    df_data = [{
        "ИНН":             c["inn"],
        "Компания":        c["name"],
        "Регион":          c["region"],
        "Выручка_млн":     round(c["revenue"] / 1e6, 1),
        "ОС_млн":          round(c["fixed_assets"] / 1e6, 1),
        "ЭЭ_млн":          round(c["electricity"] / 1e6, 1),
        "Лизинг_score":    round(c["leasing_keyword_score"], 2),
        "Найдены_слова":   c["leasing_keywords_found"],
        "Скоринг":         c["score"],
        "Приоритет":       c["priority"],
    } for c in scored]

    df = pd.DataFrame(df_data)
    out = "output/smoke_test_result.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n  CSV сохранён: {out}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
