"""
analyze_okved.py — анализ паттернов ОКВЭД среди найденных компаний.

Использование:
    python analyze_okved.py results.csv
    python analyze_okved.py results.csv --priority Горячий --top 20
    python analyze_okved.py results.csv --export okved_report.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ── Известные ОКВЭД-паттерны майнеров ───────────────────────────────────────
KNOWN_MINING_OKVEDS: dict[str, str] = {
    "62.09": "Прочая ИТ-деятельность (частый у майнеров)",
    "63.11": "Обработка данных / ЦОД",
    "26.20": "Производство ЭВМ и периферии",
    "35.11": "Производство электроэнергии",
    "35.14": "Торговля электроэнергией",
    "62.01": "Разработка ПО",
    "46.51": "Оптовая торговля компьютерами",
    "46.52": "Оптовая торговля электроникой",
    "06.10": "Добыча нефти (прикрытие)",
    "64.19": "Прочее денежное посредничество",
    "64.99": "Прочие финансовые услуги",
    "68.20": "Аренда и управление имуществом",
}


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_okveds(row: dict) -> list[str]:
    """
    Извлекает все ОКВЭДы из строки CSV.
    Поддерживает колонку okveds_all (JSON-список) и okvd_main (строка).
    """
    okveds: list[str] = []

    raw_all = row.get("okveds_all", "")
    if raw_all:
        try:
            parsed = json.loads(raw_all)
            if isinstance(parsed, list):
                okveds.extend(str(o).strip() for o in parsed if o)
        except json.JSONDecodeError:
            okveds.extend(o.strip() for o in raw_all.split(",") if o.strip())

    main = row.get("okvd_main", "").strip()
    if main and main not in okveds:
        okveds.append(main)

    return [o for o in okveds if o]


def analyze(
    rows: list[dict],
    priority_filter: str | None = None,
    top_n: int = 15,
) -> dict:
    if priority_filter:
        rows = [r for r in rows if r.get("priority", "") == priority_filter]

    total = len(rows)

    okved_counter: Counter = Counter()
    main_counter:  Counter = Counter()
    region_okved:  defaultdict = defaultdict(Counter)

    for row in rows:
        okveds = parse_okveds(row)
        region = row.get("region", "Неизвестно").strip() or "Неизвестно"

        main = row.get("okvd_main", "").strip()
        if main:
            main_counter[main] += 1

        for okved in okveds:
            okved_counter[okved] += 1
            region_okved[region][okved] += 1

    # Совпадения с известными паттернами
    pattern_hits: list[dict] = []
    for code, desc in KNOWN_MINING_OKVEDS.items():
        count = okved_counter.get(code, 0)
        if count > 0:
            pattern_hits.append({
                "okved":       code,
                "description": desc,
                "count":       count,
                "pct":         round(count / total * 100, 1) if total else 0,
            })
    pattern_hits.sort(key=lambda x: -x["count"])

    # Неожиданные ОКВЭДы (не в известных паттернах, встречаются 2+ раза)
    unexpected: list[dict] = []
    for code, count in okved_counter.most_common(top_n * 2):
        if code not in KNOWN_MINING_OKVEDS and count >= 2:
            unexpected.append({
                "okved":       code,
                "count":       count,
                "pct":         round(count / total * 100, 1) if total else 0,
                "is_main_for": main_counter.get(code, 0),
            })
        if len(unexpected) >= top_n:
            break

    return {
        "total_companies": total,
        "top_all_okveds":  okved_counter.most_common(top_n),
        "top_main_okveds": main_counter.most_common(top_n),
        "pattern_hits":    pattern_hits,
        "unexpected":      unexpected,
        "region_okved":    dict(region_okved),
        "unique_okveds":   len(okved_counter),
    }


def print_report(result: dict, priority_filter: str | None) -> None:
    SEP = "─" * 65

    print(f"\n{'═'*65}")
    print(f"  АНАЛИЗ ОКВЭД — {result['total_companies']} компаний"
          + (f" [{priority_filter}]" if priority_filter else ""))
    print(f"{'═'*65}")

    total = result["total_companies"]

    print(f"\n📊 ТОП ОКВЭД (все — включая дополнительные)")
    print(SEP)
    print(f"  {'ОКВЭД':<10} {'Компаний':>9}  {'%':>6}  Описание")
    print(SEP)
    for code, cnt in result["top_all_okveds"]:
        pct = round(cnt / total * 100, 1) if total else 0
        desc = KNOWN_MINING_OKVEDS.get(code, "")
        marker = " ⚡" if code in KNOWN_MINING_OKVEDS else ""
        print(f"  {code:<10} {cnt:>9}  {pct:>5.1f}%  {desc}{marker}")

    print(f"\n🏷️  ТОП ОСНОВНЫХ ОКВЭД (okvd_main)")
    print(SEP)
    print(f"  {'ОКВЭД':<10} {'Компаний':>9}  {'%':>6}  Описание")
    print(SEP)
    for code, cnt in result["top_main_okveds"]:
        pct = round(cnt / total * 100, 1) if total else 0
        desc = KNOWN_MINING_OKVEDS.get(code, "")
        marker = " ⚡" if code in KNOWN_MINING_OKVEDS else ""
        print(f"  {code:<10} {cnt:>9}  {pct:>5.1f}%  {desc}{marker}")

    print(f"\n✅ СОВПАДЕНИЯ С МАЙНИНГ-ПАТТЕРНАМИ")
    print(SEP)
    if result["pattern_hits"]:
        for h in result["pattern_hits"]:
            print(f"  {h['okved']:<10} {h['count']:>4} компании ({h['pct']}%)  — {h['description']}")
    else:
        print("  Нет совпадений с известными паттернами")

    print(f"\n🔍 НЕОЖИДАННЫЕ ОКВЭД (встречаются 2+ раза, не в паттернах)")
    print(SEP)
    if result["unexpected"]:
        for item in result["unexpected"]:
            print(
                f"  {item['okved']:<10} {item['count']:>4} компании ({item['pct']}%)  "
                f"[основной у {item['is_main_for']}]"
            )
    else:
        print("  Нет неожиданных ОКВЭДов")

    print(f"\n🗺️  ТОП ОКВЭД ПО РЕГИОНАМ")
    print(SEP)
    for region, counter in sorted(
        result["region_okved"].items(),
        key=lambda x: -sum(x[1].values())
    )[:8]:
        top3 = ", ".join(f"{c}({n})" for c, n in counter.most_common(3))
        print(f"  {region[:35]:<35}  →  {top3}")

    print(f"\n  Уникальных ОКВЭДов в выборке: {result['unique_okveds']}")
    print(f"{'═'*65}\n")


def export_csv(result: dict, out_path: Path) -> None:
    total = result["total_companies"]
    rows = []

    for code, cnt in result["top_all_okveds"]:
        pct = round(cnt / total * 100, 1) if total else 0
        main_cnt = dict(result["top_main_okveds"]).get(code, 0)
        rows.append({
            "okved":         code,
            "description":   KNOWN_MINING_OKVEDS.get(code, ""),
            "total_count":   cnt,
            "pct":           pct,
            "as_main_okved": main_cnt,
            "known_pattern": "да" if code in KNOWN_MINING_OKVEDS else "нет",
        })

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "okved", "description", "total_count", "pct",
            "as_main_okved", "known_pattern"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Экспорт сохранён: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Анализ паттернов ОКВЭД среди найденных компаний"
    )
    parser.add_argument("csv_file", help="CSV-файл с результатами пайплайна")
    parser.add_argument(
        "--priority", "-p",
        choices=["Горячий", "Тёплый", "Холодный"],
        help="Фильтровать только по этому приоритету",
    )
    parser.add_argument(
        "--top", "-n",
        type=int,
        default=15,
        help="Сколько топовых ОКВЭДов показывать (по умолчанию 15)",
    )
    parser.add_argument(
        "--export", "-e",
        metavar="OUTPUT.CSV",
        help="Сохранить результат в CSV",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"❌ Файл не найден: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = load_csv(csv_path)
    if not rows:
        print("❌ CSV пустой", file=sys.stderr)
        sys.exit(1)

    print(f"📂 Загружено {len(rows)} строк из {csv_path.name}")

    result = analyze(rows, priority_filter=args.priority, top_n=args.top)
    print_report(result, priority_filter=args.priority)

    if args.export:
        export_csv(result, Path(args.export))


if __name__ == "__main__":
    main()
