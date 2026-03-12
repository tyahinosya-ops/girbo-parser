"""
ГИРБО Парсер — поиск майнинговых компаний и хостингов по расходам на электроэнергию
====================================================================================
Категории:
    hosting — поиск по ОКВЭД через ЕГРЮЛ, фильтр расходов ЭЭ > 10 млн руб/мес
    mining  — поиск лизинговых договоров на Федресурсе, проверка активности

Требования:
    pip install requests pandas lxml openpyxl tqdm
"""

import argparse
import json
import logging
import re
import time
import random
from functools import wraps
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(message)s")
log = logging.getLogger(__name__)

# ── ОКВЭД для хостинга / ЦОД ──────────────────────────────
HOSTING_OKVEDS = [
    "63.11",  # Обработка данных, хостинг
    "62.09",  # Прочая IT-деятельность
    "62.01",  # Разработка ПО
    "35.11",  # Производство электроэнергии
    "35.14",  # Торговля электроэнергией
    "46.51",  # Оптовая торговля компьютерами
    "46.52",  # Оптовая торговля электроникой
]

# ── Регионы (коды ЕГРЮЛ) ──────────────────────────────────
TARGET_REGIONS = ["38", "24", "19", "03", "10", "07", "05"]

# ── Ключевые слова для поиска майнеров на Федресурсе ──────
MINING_KEYWORDS = [
    "ASIC майнер", "Antminer", "Whatsminer", "MicroBT", "Bitmain",
    "майнинг оборудование", "криптовалюта лизинг",
    "GPU ферма", "bitcoin mining", "добыча криптовалюты",
]

MIN_ELECTRICITY_EXPENSE = 10_000_000   # 10 млн руб/мес
REPORT_YEAR = 2023
REQUEST_DELAY = 0.7
OUTPUT_DIR = "output"

ELECTRICITY_KEYWORDS = [
    "электроэнерги", "электр. энерг", "э/э",
    "коммунальные услуги", "потребление энергии", "энергоносител",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _ua():
    return random.choice(USER_AGENTS)


def _sleep(lo=0.5, hi=1.5):
    time.sleep(random.uniform(lo, hi))


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": _ua(),
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
                    log.debug(f"Попытка {attempt + 1}/{max_attempts}: {exc}. Жду {wait:.1f}с")
                    time.sleep(wait)
            log.warning(f"Все {max_attempts} попытки исчерпаны: {last_exc}")
            return None
        return wrapper
    return decorator


_session = make_session()


# ══════════════════════════════════════════════════════════
# КАТЕГОРИЯ 1: ХОСТИНГ — поиск через ЕГРЮЛ по ОКВЭД
# ══════════════════════════════════════════════════════════

def _init_egrul_session():
    """Инициализирует сессию ЕГРЮЛ (получает cookies через homepage)."""
    try:
        _session.get(
            "https://egrul.nalog.ru/",
            headers={
                "User-Agent": _ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            timeout=15,
        )
        _sleep(1.5, 3.0)
        log.info("Сессия egrul.nalog.ru инициализирована.")
    except Exception as e:
        log.warning(f"Ошибка инициализации ЕГРЮЛ: {e}")


@with_retry(max_attempts=3, backoff=2.0)
def _egrul_get_token(region: str, okvd: str) -> str | None:
    """
    Шаг 1: POST / → получает поисковый токен.
    ЕГРЮЛ API двухшаговый: POST даёт токен, GET /search-result?t=TOKEN даёт строки.
    """
    okvd_clean = okvd.replace(".", "")
    headers = {
        "User-Agent":       _ua(),
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin":           "https://egrul.nalog.ru",
        "Referer":          "https://egrul.nalog.ru/",
    }
    for payload in [
        {"query": "", "region": region, "okvedCodes": okvd_clean, "vo": ""},
        {"query": "", "region": region, "okvedCodes": okvd,       "vo": ""},
        {"query": "", "region": region, "okvedCodes": okvd_clean, "vo": "ul"},
    ]:
        resp = _session.post("https://egrul.nalog.ru/", data=payload,
                             headers=headers, timeout=15)
        if resp.status_code == 200:
            token = resp.json().get("t")
            if token:
                return token
        _sleep(0.5, 1.0)
    return None


@with_retry(max_attempts=3, backoff=2.0)
def _egrul_fetch_page(token: str, page: int = 1) -> tuple[list, int]:
    """Шаг 2: GET /search-result?t=TOKEN&p=PAGE → (строки, всего)."""
    resp = _session.get(
        "https://egrul.nalog.ru/search-result",
        params={"t": token, "r": "y", "p": page},
        headers={
            "User-Agent":       _ua(),
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer":          "https://egrul.nalog.ru/",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data  = resp.json()
    rows  = data.get("rows", [])
    total = data.get("cnt", len(rows))
    return rows, total


def get_inns_by_okveds(okveds: list, regions: list) -> list[str]:
    """Собирает ИНН хостинговых компаний из ЕГРЮЛ по ОКВЭД + регионам."""
    _init_egrul_session()
    inns: set[str] = set()

    for okvd in okveds:
        for region in regions:
            log.info(f"ЕГРЮЛ: ОКВЭД {okvd} / регион {region}")

            token = _egrul_get_token(region, okvd)
            if not token:
                continue

            _sleep(0.8, 1.5)
            first_rows, total = _egrul_fetch_page(token, page=1)
            log.info(f"  Всего: {total}, стр.1: {len(first_rows)}")

            for row in first_rows:
                inn = str(row.get("i", "")).strip()
                if inn:
                    inns.add(inn)

            # Докачиваем страницы (до 10)
            total_pages = min((total // 20) + 1, 10)
            for page in range(2, total_pages + 1):
                _sleep(0.5, 1.2)
                more_rows, _ = _egrul_fetch_page(token, page=page)
                if not more_rows:
                    break
                for row in more_rows:
                    inn = str(row.get("i", "")).strip()
                    if inn:
                        inns.add(inn)

            _sleep(2.0, 4.0)

    result = list(inns)
    log.info(f"ЕГРЮЛ: уникальных ИНН = {len(result)}")
    return result


# ══════════════════════════════════════════════════════════
# КАТЕГОРИЯ 2: МАЙНЕРЫ — поиск лизинга через Федресурс
# ══════════════════════════════════════════════════════════

INN_RE = re.compile(r"(?:ИНН|inn)[:\s]*(\d{10,12})", re.IGNORECASE)
FEDRESURS_ENDPOINTS = [
    "https://fedresurs.ru/backend/search",
    "https://fedresurs.ru/backend/efrs-messages",
]


def _extract_inn_fedresurs(item: dict) -> str:
    for field in ("entityInn", "inn", "companyInn", "participantInn"):
        val = str(item.get(field, "")).strip()
        if re.fullmatch(r"\d{10,12}", val):
            return val
    text = " ".join(str(item.get(k, "")) for k in
                    ("messageText", "text", "title", "description", "entityName"))
    m = INN_RE.search(text)
    return m.group(1) if m else ""


def _fedresurs_search(keyword: str, fed_session: requests.Session) -> list[str]:
    """Ищет ИНН на Федресурсе по ключевому слову."""
    endpoint = None
    for ep in FEDRESURS_ENDPOINTS:
        try:
            r = fed_session.get(ep, params={"searchString": keyword, "limit": 5, "offset": 0},
                                 headers={"User-Agent": _ua(), "Accept": "application/json",
                                          "Referer": "https://fedresurs.ru/"}, timeout=20)
            if r.status_code == 200:
                endpoint = ep
                break
        except Exception:
            continue

    if not endpoint:
        return []

    inns = []
    offset, limit = 0, 40

    while offset < 200:
        try:
            r = fed_session.get(
                endpoint,
                params={"searchString": keyword, "limit": limit, "offset": offset},
                headers={"User-Agent": _ua(), "Accept": "application/json",
                         "Origin": "https://fedresurs.ru", "Referer": "https://fedresurs.ru/"},
                timeout=20,
            )
            if r.status_code != 200:
                break

            data  = r.json()
            items = data.get("data") or data.get("items") or data.get("content") or []
            total = data.get("total", data.get("totalElements", 0)) or len(items)

            if not items:
                break

            for item in items:
                inn = _extract_inn_fedresurs(item)
                if inn:
                    inns.append(inn)

            offset += limit
            if offset >= total:
                break

            _sleep(0.8, 1.5)
        except Exception as e:
            log.debug(f"Федресурс ошибка: {e}")
            break

    return inns


def get_inns_from_fedresurs(keywords: list[str] = None) -> list[str]:
    """Ищет ИНН майнинговых компаний через лизинговые договоры на Федресурсе."""
    keywords = keywords or MINING_KEYWORDS
    fed_session = requests.Session()
    seen: set[str] = set()

    log.info(f"Федресурс: поиск по {len(keywords)} ключевым словам")

    for kw in keywords:
        log.info(f"  → «{kw}»")
        inns = _fedresurs_search(kw, fed_session)
        log.info(f"    Найдено ИНН: {len(inns)}")
        seen.update(inns)
        _sleep(2.0, 3.5)

    result = list(seen)
    log.info(f"Федресурс: уникальных ИНН = {len(result)}")
    return result


# ══════════════════════════════════════════════════════════
# ГИР БО (bo.nalog.ru) — финансовая отчётность
# ══════════════════════════════════════════════════════════

@with_retry(max_attempts=3, backoff=2.0)
def _girbo_search(inn: str) -> list:
    resp = _session.get("https://bo.nalog.ru/nbo/organizations/search",
                        params={"query": inn, "page": 0, "size": 5}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("content", [])


@with_retry(max_attempts=3, backoff=2.0)
def _girbo_get_bfo_list(org_id: int) -> list:
    resp = _session.get(f"https://bo.nalog.ru/nbo/organizations/{org_id}/bfo/", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("content", [data])


@with_retry(max_attempts=3, backoff=2.0)
def _girbo_get_bfo_detail(bfo_id: int) -> dict:
    resp = _session.get(f"https://bo.nalog.ru/nbo/bfo/{bfo_id}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_report_from_girbo(inn: str, year: int = REPORT_YEAR) -> dict | None:
    try:
        orgs = _girbo_search(inn)
        if not orgs:
            return None
        org    = orgs[0]
        org_id = org.get("id")
        if not org_id:
            return None

        bfo_list = _girbo_get_bfo_list(org_id)
        if not bfo_list:
            return None

        target_bfo = next(
            (b for b in bfo_list if str(b.get("year") or b.get("reportYear") or "") == str(year)),
            bfo_list[0],
        )

        bfo_id        = target_bfo.get("id")
        report_detail = _girbo_get_bfo_detail(bfo_id) if bfo_id else target_bfo

        return {
            "inn":      inn,
            "org_name": org.get("shortName") or org.get("name", ""),
            "region":   org.get("region", ""),
            "okvd_main":org.get("okved", ""),
            "report":   report_detail,
        }
    except Exception as e:
        log.debug(f"ИНН {inn}: ошибка ГИРБО — {e}")
        return None


def _parse_number(s: str) -> float | None:
    s = re.sub(r"[\s\xa0]", "", str(s)).replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def extract_electricity_expenses(report_data: dict) -> float:
    if not report_data or "report" not in report_data:
        return 0.0
    try:
        report_str = json.dumps(report_data["report"], ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return 0.0

    best = 0.0
    num_pat = r"[\d][\d\s\xa0]*(?:[.,][\d]+)?"

    for kw in ELECTRICITY_KEYWORDS:
        if kw.lower() not in report_str:
            continue
        for m in re.finditer(rf"{re.escape(kw.lower())}.{{0,150}}?({num_pat})", report_str):
            v = _parse_number(m.group(1))
            if v and 100 < v * 1000 < 1e13:
                best = max(best, v * 1000)
    return best


FORM_CODES = {
    "2110": "revenue",
    "2120": "cost_of_sales",
    "1150": "fixed_assets",
    "1600": "balance_total",
    "2400": "net_profit",
}


def extract_key_financials(report_data: dict) -> dict:
    result = {k: 0.0 for k in FORM_CODES.values()}
    result["employees"] = 0

    if not report_data or "report" not in report_data:
        return result

    try:
        report  = report_data["report"]
        periods = report if isinstance(report, list) else [report]

        for period in periods:
            if not isinstance(period, dict):
                continue
            for form in period.get("forms", [period]):
                if not isinstance(form, dict):
                    continue
                for row in form.get("rows", []):
                    code = str(row.get("code", "")).strip()
                    if code not in FORM_CODES:
                        continue
                    raw = row.get("currentPeriodValue") or row.get("value") or 0
                    try:
                        result[FORM_CODES[code]] = abs(float(raw)) * 1000
                    except (TypeError, ValueError):
                        pass

            emp = period.get("averageNumberOfEmployees", 0)
            if emp:
                result["employees"] = int(emp)
    except Exception as e:
        log.debug(f"Ошибка парсинга финансов: {e}")

    return result


def calculate_mining_score(financials: dict, electricity: float) -> tuple[int, list]:
    score, triggers = 0, []

    rev  = financials.get("revenue", 0)
    cost = financials.get("cost_of_sales", 0)
    fa   = financials.get("fixed_assets", 0)
    bal  = financials.get("balance_total", 0)
    emp  = financials.get("employees", 0)
    net  = financials.get("net_profit", 0)

    if electricity >= 50_000_000:
        score += 30; triggers.append(f"ЭЭ > 50 млн (+30): {electricity/1e6:.1f} млн")
    elif electricity >= 10_000_000:
        score += 20; triggers.append(f"ЭЭ > 10 млн (+20): {electricity/1e6:.1f} млн")
    elif electricity >= 5_000_000:
        score += 10; triggers.append(f"ЭЭ > 5 млн (+10): {electricity/1e6:.1f} млн")
    elif electricity > 0:
        score += 5;  triggers.append(f"ЭЭ найдена (+5): {electricity/1e6:.1f} млн")

    if rev > 0:
        ratio = cost / rev
        if ratio > 0.80:
            score += 20; triggers.append(f"Себест./Выручка > 80% (+20): {ratio:.0%}")
        elif ratio > 0.70:
            score += 15; triggers.append(f"Себест./Выручка > 70% (+15): {ratio:.0%}")
        elif ratio > 0.60:
            score += 8;  triggers.append(f"Себест./Выручка > 60% (+8): {ratio:.0%}")

    if bal > 0:
        fa_r = fa / bal
        if fa_r > 0.60:
            score += 20; triggers.append(f"ОС/Баланс > 60% (+20): {fa_r:.0%}")
        elif fa_r > 0.40:
            score += 12; triggers.append(f"ОС/Баланс > 40% (+12): {fa_r:.0%}")

    if emp > 0 and rev > 0:
        rpe = rev / emp
        if rpe > 20_000_000:
            score += 15; triggers.append(f"Выручка/сотр > 20 млн (+15): {rpe/1e6:.1f} млн")
        elif rpe > 10_000_000:
            score += 8;  triggers.append(f"Выручка/сотр > 10 млн (+8): {rpe/1e6:.1f} млн")
    elif emp == 0 and rev > 5_000_000:
        score += 10; triggers.append("Нет сотрудников при выручке > 5 млн (+10)")

    if rev > 10_000_000 and net < rev * 0.05:
        score += 5; triggers.append("Низкая рентабельность <5% (+5)")

    return min(score, 100), triggers


def get_priority_label(score: int) -> str:
    if score >= 70: return "Горячий"
    elif score >= 40: return "Тёплый"
    else: return "Холодный"


# ══════════════════════════════════════════════════════════
# Загрузка ИНН из файла
# ══════════════════════════════════════════════════════════

def load_inns_from_file(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {filepath}")
    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath, dtype=str)
        col = next((c for c in df.columns if "инн" in c.lower() or "inn" in c.lower()), df.columns[0])
        inns = df[col].str.strip().tolist()
    else:
        with open(filepath, encoding="utf-8") as f:
            inns = [l.strip() for l in f if l.strip()]
    valid = [i for i in inns if i.isdigit() and len(i) in (10, 12)]
    log.info(f"Загружено из файла: {len(valid)} ИНН (невалидных: {len(inns)-len(valid)})")
    return valid


# ══════════════════════════════════════════════════════════
# Основной пайплайн
# ══════════════════════════════════════════════════════════

def run_parser(
    category: str = "hosting",
    inn_source: str = "api",
    min_electricity: float = MIN_ELECTRICITY_EXPENSE,
    year: int = REPORT_YEAR,
    output: str = None,
    min_score: int = 30,
):
    Path(OUTPUT_DIR).mkdir(exist_ok=True)

    # ── Шаг 1: Получение ИНН ──────────────────────────────
    log.info(f"Шаг 1: получение ИНН (категория={category}, источник={inn_source})")

    if inn_source != "api":
        inns = load_inns_from_file(inn_source)
    elif category == "mining":
        inns = get_inns_from_fedresurs()
    else:
        inns = get_inns_by_okveds(HOSTING_OKVEDS, TARGET_REGIONS)

    if not inns:
        log.error("ИНН не получены.")
        return None

    log.info(f"ИНН для анализа: {len(inns)}")

    # ── Шаг 2: Анализ отчётности ──────────────────────────
    log.info("Шаг 2: анализ бухотчётности через ГИР БО...")
    results = []

    for inn in tqdm(inns, desc="Анализ"):
        time.sleep(REQUEST_DELAY)

        report_data = get_report_from_girbo(inn, year=year)
        if not report_data:
            continue

        electricity      = extract_electricity_expenses(report_data)
        financials       = extract_key_financials(report_data)
        score, triggers  = calculate_mining_score(financials, electricity)

        if financials["revenue"] < 1_000_000:
            continue

        # Фильтр для хостинга: прокси ЭЭ или скоринг
        if category == "hosting":
            proxy_monthly = financials["cost_of_sales"] * 0.40 / 12
            if electricity < min_electricity and proxy_monthly < min_electricity and score < min_score:
                continue
        # Для майнеров: ИНН уже отфильтрованы Федресурсом — берём всех

        results.append({
            "ИНН":                 inn,
            "Категория":           "Хостинг" if category == "hosting" else "Майнинг",
            "Компания":            report_data.get("org_name", ""),
            "Регион":              report_data.get("region", ""),
            "ОКВЭД":               report_data.get("okvd_main", ""),
            "Расходы_ЭЭ_руб":     int(electricity),
            "Прокси_ЭЭ_мес_руб":  int(financials["cost_of_sales"] * 0.40 / 12),
            "Выручка_руб":         int(financials["revenue"]),
            "Себестоимость_руб":   int(financials["cost_of_sales"]),
            "ОС_руб":              int(financials["fixed_assets"]),
            "Баланс_руб":          int(financials["balance_total"]),
            "Чистая_прибыль_руб":  int(financials["net_profit"]),
            "Сотрудников":         financials["employees"],
            "Скоринг_майнинг":     score,
            "Приоритет":           get_priority_label(score),
            "Триггеры":            " | ".join(triggers),
        })

    if not results:
        log.warning("Компаний не найдено по критериям.")
        return None

    df = pd.DataFrame(results)
    df = df.sort_values("Скоринг_майнинг", ascending=False).reset_index(drop=True)

    from datetime import date
    today  = date.today().strftime("%Y-%m-%d")
    suffix = "hosting" if category == "hosting" else "mining"
    out    = output or f"{OUTPUT_DIR}/mining_leads_{suffix}_{today}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")

    inn_path = f"{OUTPUT_DIR}/inns_{suffix}_{today}.txt"
    df["ИНН"].dropna().to_csv(inn_path, index=False, header=False)

    hot  = len(df[df["Приоритет"] == "Горячий"])
    warm = len(df[df["Приоритет"] == "Тёплый"])
    cold = len(df[df["Приоритет"] == "Холодный"])

    cat_label = "ХОСТИНГ / ЦОД" if category == "hosting" else "МАЙНЕРЫ"
    log.info("=" * 55)
    log.info(f"  {cat_label} — найдено компаний: {len(df)}")
    log.info(f"  Горячих лидов:  {hot}")
    log.info(f"  Тёплых лидов:   {warm}")
    log.info(f"  Холодных лидов: {cold}")
    log.info(f"  CSV: {out}")
    log.info(f"  ИНН: {inn_path}")
    log.info("=" * 55)

    return df


def parse_args():
    parser = argparse.ArgumentParser(description="Парсер майнинговых компаний и хостингов (ГИРБО)")
    parser.add_argument("--category", choices=["hosting", "mining"], default="hosting",
        help="Категория: hosting (хостинг/ЦОД по ОКВЭД) или mining (майнеры через Федресурс)")
    parser.add_argument("--source", default="api",
        help='"api" — ЕГРЮЛ/Федресурс, или путь к файлу inns.txt')
    parser.add_argument("--min-ee", type=float, default=MIN_ELECTRICITY_EXPENSE,
        dest="min_electricity", help="Минимальные расходы на ЭЭ, руб/мес")
    parser.add_argument("--year", type=int, default=REPORT_YEAR, help="Год отчётности")
    parser.add_argument("--output", default=None, help="Путь к итоговому CSV")
    parser.add_argument("--min-score", type=int, default=30, dest="min_score")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_parser(
        category=args.category,
        inn_source=args.source,
        min_electricity=args.min_electricity,
        year=args.year,
        output=args.output,
        min_score=args.min_score,
    )
