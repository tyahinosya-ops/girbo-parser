"""
ГИРБО Парсер — поиск майнинговых компаний по расходам на электроэнергию
=======================================================================
Требования:
    pip install requests pandas lxml openpyxl tqdm
"""

import argparse
import json
import logging
import re
import time
from functools import wraps
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(message)s")
log = logging.getLogger(__name__)

OKVEDS = [
    "63.11", "62.09", "62.01", "64.19", "66.19", "35.11", "35.14",
]

TARGET_REGIONS = ["38", "24", "19", "03", "10", "07"]

MIN_ELECTRICITY_EXPENSE = 5_000_000
REPORT_YEAR = 2023
REQUEST_DELAY = 0.7
OUTPUT_FILE = "mining_leads.csv"

ELECTRICITY_KEYWORDS = [
    "электроэнерги",
    "электр. энерг",
    "э/э",
    "коммунальные услуги",
    "потребление энергии",
    "энергоносител",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    return session


def with_retry(max_attempts: int = 3, backoff: float = 2.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (requests.RequestException, ValueError, KeyError) as exc:
                    last_exc = exc
                    wait = backoff ** attempt
                    log.debug(f"Попытка {attempt + 1}/{max_attempts} неудачна: {exc}. Жду {wait:.1f}с")
                    time.sleep(wait)
            log.warning(f"Все {max_attempts} попытки исчерпаны: {last_exc}")
            return None
        return wrapper
    return decorator


_session = make_session()


@with_retry(max_attempts=3, backoff=2.0)
def _egrul_search_page(okvd: str, region: str, page: int) -> list:
    url = "https://egrul.nalog.ru/"
    payload = {
        "query": "",
        "region": region,
        "okvedCodes": okvd,
        "page": page,
    }
    resp = _session.post(url, data=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("rows", [])


def get_inns_by_okveds(okveds: list, regions: list) -> list:
    inns: list[str] = []
    seen: set[str] = set()

    for okvd in okveds:
        for region in regions:
            page = 0
            while True:
                rows = _egrul_search_page(okvd, region, page)
                if not rows:
                    break

                added = 0
                for company in rows:
                    inn = str(company.get("i", "")).strip()
                    if inn and inn not in seen:
                        seen.add(inn)
                        inns.append(inn)
                        added += 1

                log.info(
                    f"ОКВЭД {okvd} / Регион {region} / стр.{page}: "
                    f"+{added} компаний (всего {len(inns)})"
                )
                if len(rows) < 20:
                    break
                page += 1
                time.sleep(REQUEST_DELAY)

    log.info(f"Всего уникальных ИНН: {len(inns)}")
    return inns


def load_inns_from_file(filepath: str) -> list:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {filepath}")

    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath, dtype=str)
        inn_col = next(
            (c for c in df.columns if "инн" in c.lower() or "inn" in c.lower()),
            df.columns[0],
        )
        return df[inn_col].str.strip().tolist()
    else:
        with open(filepath, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]


@with_retry(max_attempts=3, backoff=2.0)
def _girbo_search(inn: str) -> list:
    resp = _session.get(
        "https://bo.nalog.ru/nbo/organizations/search",
        params={"query": inn, "page": 0, "size": 5},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("content", [])


@with_retry(max_attempts=3, backoff=2.0)
def _girbo_get_bfo_list(org_id: int) -> list:
    resp = _session.get(
        f"https://bo.nalog.ru/nbo/organizations/{org_id}/bfo",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("content", [data])


@with_retry(max_attempts=3, backoff=2.0)
def _girbo_get_bfo_detail(bfo_id: int) -> dict:
    resp = _session.get(
        f"https://bo.nalog.ru/nbo/bfo/{bfo_id}",
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_report_from_girbo(inn: str, year: int = REPORT_YEAR) -> dict | None:
    try:
        orgs = _girbo_search(inn)
        if not orgs:
            return None

        org = orgs[0]
        org_id = org.get("id")
        if not org_id:
            return None

        bfo_list = _girbo_get_bfo_list(org_id)
        if not bfo_list:
            return None

        target_bfo = None
        for bfo in bfo_list:
            bfo_year = bfo.get("year") or bfo.get("reportYear") or bfo.get("period")
            if str(bfo_year) == str(year):
                target_bfo = bfo
                break

        if target_bfo is None:
            target_bfo = bfo_list[0]
            log.debug(f"ИНН {inn}: год {year} не найден, использую последний")

        bfo_id = target_bfo.get("id")
        report_detail = _girbo_get_bfo_detail(bfo_id) if bfo_id else target_bfo

        return {
            "inn": inn,
            "org_name": org.get("shortName") or org.get("name", ""),
            "region": org.get("region", ""),
            "okvd_main": org.get("okved", ""),
            "report": report_detail,
        }

    except Exception as e:
        log.debug(f"ИНН {inn}: ошибка получения отчётности — {e}")
        return None


def _parse_russian_number(s: str) -> float | None:
    s = re.sub(r"[\s\xa0]", "", s)
    s = s.replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_electricity_expenses(report_data: dict) -> float:
    if not report_data or "report" not in report_data:
        return 0.0

    try:
        report_str = json.dumps(report_data["report"], ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return 0.0

    best_amount = 0.0
    number_pattern = r"[\d][\d\s\xa0]*(?:[.,][\d]+)?"

    for keyword in ELECTRICITY_KEYWORDS:
        keyword_lower = keyword.lower()
        if keyword_lower not in report_str:
            continue

        pattern = rf"{re.escape(keyword_lower)}.{{0,150}}?({number_pattern})"
        for match in re.finditer(pattern, report_str):
            amount = _parse_russian_number(match.group(1))
            if amount is None:
                continue
            amount_rub = amount * 1_000
            if 100 < amount_rub < 1e13:
                best_amount = max(best_amount, amount_rub)

    return best_amount


def extract_key_financials(report_data: dict) -> dict:
    financials = {
        "revenue": 0.0,
        "cost_of_sales": 0.0,
        "fixed_assets": 0.0,
        "balance_total": 0.0,
        "employees": 0,
    }

    if not report_data or "report" not in report_data:
        return financials

    try:
        report = report_data["report"]
        periods = report if isinstance(report, list) else [report]

        for period in periods:
            if not isinstance(period, dict):
                continue
            forms = period.get("forms", [period])

            for form in forms:
                if not isinstance(form, dict):
                    continue
                for row in form.get("rows", []):
                    if not isinstance(row, dict):
                        continue

                    code = str(row.get("code", "")).strip()
                    raw_value = (
                        row.get("currentPeriodValue")
                        or row.get("value")
                        or row.get("sum")
                        or 0
                    )
                    try:
                        value = float(raw_value) * 1_000
                    except (TypeError, ValueError):
                        value = 0.0

                    if code == "2110":
                        financials["revenue"] = value
                    elif code == "2120":
                        financials["cost_of_sales"] = abs(value)
                    elif code == "1150":
                        financials["fixed_assets"] = value
                    elif code == "1600":
                        financials["balance_total"] = value

    except Exception as e:
        log.debug(f"Ошибка парсинга финансов: {e}")

    return financials


def calculate_mining_score(financials: dict, electricity: float) -> int:
    score = 0

    rev = financials.get("revenue", 0.0)
    costs = financials.get("cost_of_sales", 0.0)
    fa = financials.get("fixed_assets", 0.0)
    bal = financials.get("balance_total", 0.0)
    employees = financials.get("employees", 0)

    if electricity >= 50_000_000:
        score += 30
    elif electricity >= 10_000_000:
        score += 20
    elif electricity >= 5_000_000:
        score += 10

    if electricity > 0:
        score += 10

    if rev > 0 and costs > 0:
        if costs / rev > 0.85:
            score += 15
        elif costs / rev > 0.7:
            score += 8

    if bal > 0 and fa > 0:
        fa_ratio = fa / bal
        if fa_ratio > 0.7:
            score += 20
        elif fa_ratio > 0.5:
            score += 12

    if employees < 10 and rev > 10_000_000:
        score += 15

    return min(score, 100)


def run_parser(
    inn_source: str = "api",
    min_electricity: float = MIN_ELECTRICITY_EXPENSE,
    year: int = REPORT_YEAR,
    output: str = OUTPUT_FILE,
    min_score: int = 30,
):
    results = []

    log.info("Шаг 1: Загрузка ИНН...")
    if inn_source == "api":
        inns = get_inns_by_okveds(OKVEDS, TARGET_REGIONS)
    else:
        inns = load_inns_from_file(inn_source)

    if not inns:
        log.error("Не удалось получить ни одного ИНН.")
        return None

    log.info(f"Загружено {len(inns)} ИНН для обработки")
    log.info("Шаг 2–4: Анализ отчётности...")

    for inn in tqdm(inns, desc="Обработка"):
        time.sleep(REQUEST_DELAY)

        report_data = get_report_from_girbo(inn, year=year)
        if not report_data:
            continue

        electricity = extract_electricity_expenses(report_data)
        financials = extract_key_financials(report_data)
        score = calculate_mining_score(financials, electricity)

        if electricity < min_electricity and score < min_score:
            continue

        if score >= 70:
            priority = "Горячий"
        elif score >= 40:
            priority = "Теплый"
        else:
            priority = "Холодный"

        results.append({
            "ИНН": inn,
            "Компания": report_data.get("org_name", ""),
            "Регион": report_data.get("region", ""),
            "ОКВЭД": report_data.get("okvd_main", ""),
            "Расходы_ЭЭ_руб": int(electricity),
            "Выручка_руб": int(financials["revenue"]),
            "Себестоимость_руб": int(financials["cost_of_sales"]),
            "ОС_руб": int(financials["fixed_assets"]),
            "Баланс_руб": int(financials["balance_total"]),
            "Скоринг_майнинг": score,
            "Приоритет": priority,
        })

    if not results:
        log.warning("Не найдено компаний по заданным критериям.")
        return None

    df = pd.DataFrame(results)
    df = df.sort_values("Скоринг_майнинг", ascending=False)
    df.to_csv(output, index=False, encoding="utf-8-sig")

    hot = len(df[df["Приоритет"] == "Горячий"])
    warm = len(df[df["Приоритет"] == "Теплый"])

    log.info("=" * 50)
    log.info(f"Готово! Найдено компаний: {len(df)}")
    log.info(f"Горячих лидов:  {hot}")
    log.info(f"Теплых лидов:   {warm}")
    log.info(f"Результат сохранён: {output}")
    log.info("=" * 50)

    return df


def parse_args():
    parser = argparse.ArgumentParser(
        description="Парсер майнинговых компаний (ГИРБО)"
    )
    parser.add_argument("--source", default="api",
        help='"api" или путь к файлу inns.txt / inns.csv')
    parser.add_argument("--min-ee", type=float, default=MIN_ELECTRICITY_EXPENSE,
        help="Минимальные расходы на ЭЭ, руб.")
    parser.add_argument("--year", type=int, default=REPORT_YEAR,
        help="Год отчётности")
    parser.add_argument("--output", default=OUTPUT_FILE,
        help="Путь к итоговому CSV")
    parser.add_argument("--min-score", type=int, default=30,
        help="Минимальный скоринговый балл")
    parser.add_argument("--debug", action="store_true",
        help="Подробное логирование")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_parser(
        inn_source=args.source,
        min_electricity=args.min_ee,
        year=args.year,
        output=args.output,
        min_score=args.min_score,
    )
