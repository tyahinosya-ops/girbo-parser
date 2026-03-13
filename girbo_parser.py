"""
ГИРБО Парсер — поиск хостинговых и майнинговых компаний
=========================================================
Категории:
    hosting — поиск по ОКВЭД через ЕГРЮЛ, фильтр расходов ЭЭ
    mining  — поиск лизинговых договоров на Федресурсе

Требования:
    pip install requests pandas lxml openpyxl tqdm
"""

import argparse
import json
import logging
import re
import sys
import time
import random
from datetime import date
from functools import wraps
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────
HOSTING_OKVEDS = [
    "63.11", "62.09", "62.01",
    "35.11", "35.14",
    "46.51", "46.52",
]

TARGET_REGIONS = ["38", "24", "19", "03", "10", "07", "05"]

MINING_KEYWORDS = [
    "Antminer", "Whatsminer", "MicroBT", "Bitmain",
    "ASIC майнер", "майнинг оборудование",
    "GPU ферма", "bitcoin mining", "добыча криптовалюты",
    "криптовалюта лизинг",
]

MIN_ELECTRICITY_EXPENSE = 5_000_000
REPORT_YEAR = 2023
REQUEST_DELAY = 0.5
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


def _sleep(lo=0.4, hi=1.2):
    time.sleep(random.uniform(lo, hi))


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":    _ua(),
        "Accept":        "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    return s


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
                    log.debug(f"Попытка {attempt+1}/{max_attempts}: {exc}. Жду {wait:.1f}с")
                    time.sleep(wait)
            log.warning(f"Все {max_attempts} попытки исчерпаны: {last_exc}")
            return None
        return wrapper
    return decorator


_session = make_session()
_stats: dict = {}


def _inc(key: str, n: int = 1):
    _stats[key] = _stats.get(key, 0) + n


# ══════════════════════════════════════════════════════════
# КАТЕГОРИЯ 1: ХОСТИНГ — ЕГРЮЛ
# ══════════════════════════════════════════════════════════

def _init_egrul_session():
    try:
        resp = _session.get(
            "https://egrul.nalog.ru/",
            headers={
                "User-Agent": _ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=15,
        )
        log.info(f"ЕГРЮЛ init: HTTP {resp.status_code}, cookies={list(resp.cookies.keys())}")
        _sleep(1.5, 3.0)
    except Exception as e:
        log.warning(f"ЕГРЮЛ init ошибка: {e}")


@with_retry(max_attempts=3, backoff=2.0)
def _egrul_get_token(region: str, okvd: str) -> str | None:
    """POST → получает поисковый токен."""
    okvd_nodot = okvd.replace(".", "")
    headers = {
        "User-Agent":       _ua(),
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin":           "https://egrul.nalog.ru",
        "Referer":          "https://egrul.nalog.ru/",
    }
    payloads = [
        {"query": "", "region": region, "okvedCodes": okvd_nodot, "vo": "ul"},
        {"query": "", "region": region, "okvedCodes": okvd,       "vo": "ul"},
        {"query": "", "region": region, "okvedCodes": okvd_nodot, "vo": ""},
    ]
    for payload in payloads:
        resp = _session.post(
            "https://egrul.nalog.ru/",
            data=payload, headers=headers, timeout=15,
        )
        log.debug(f"ЕГРЮЛ POST {region}/{okvd}: HTTP {resp.status_code}, body={resp.text[:120]}")
        if resp.status_code == 200:
            token = resp.json().get("t")
            if token:
                log.debug(f"ЕГРЮЛ token получен: {token[:20]}...")
                _inc("egrul_token_ok")
                return token
        _sleep(0.5, 1.0)
    log.warning(f"ЕГРЮЛ: не получен токен для {region}/{okvd}")
    _inc("egrul_token_fail")
    return None


@with_retry(max_attempts=3, backoff=2.0)
def _egrul_fetch_page(token: str, page: int = 1) -> tuple[list, int]:
    """GET /search-result?t=TOKEN&p=PAGE → (строки, всего)."""
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
    total = int(data.get("cnt", len(rows)) or 0)
    log.debug(f"ЕГРЮЛ стр.{page}: rows={len(rows)}, total={total}")
    return rows, total


def get_inns_by_okveds(okveds: list, regions: list) -> list[str]:
    _init_egrul_session()
    inns: set[str] = set()

    for okvd in okveds:
        for region in regions:
            log.info(f"ЕГРЮЛ: ОКВЭД {okvd} / регион {region}")
            token = _egrul_get_token(region, okvd)
            if not token:
                continue

            _sleep(0.8, 1.5)
            try:
                first_rows, total = _egrul_fetch_page(token, page=1)
            except Exception as e:
                log.warning(f"  Ошибка страницы 1: {e}")
                continue

            log.info(f"  Всего: {total}, стр.1: {len(first_rows)}")
            for row in first_rows:
                inn = str(row.get("i", "")).strip()
                if inn:
                    inns.add(inn)

            total_pages = min((total // 20) + 1, 10)
            for page in range(2, total_pages + 1):
                _sleep(0.5, 1.2)
                try:
                    more_rows, _ = _egrul_fetch_page(token, page=page)
                    if not more_rows:
                        break
                    for row in more_rows:
                        inn = str(row.get("i", "")).strip()
                        if inn:
                            inns.add(inn)
                except Exception as e:
                    log.warning(f"  Ошибка стр.{page}: {e}")
                    break

            _sleep(2.0, 4.0)

    result = list(inns)
    log.info(f"ЕГРЮЛ итого: {len(result)} уникальных ИНН")
    _inc("egrul_inns", len(result))
    return result


# ══════════════════════════════════════════════════════════
# КАТЕГОРИЯ 2: МАЙНЕРЫ — Федресурс
# ══════════════════════════════════════════════════════════

INN_RE = re.compile(r"(?:ИНН|inn)[:\s]*(\d{10,12})", re.IGNORECASE)
FEDRESURS_ENDPOINTS = [
    "https://fedresurs.ru/backend/search",
    "https://fedresurs.ru/backend/efrs-messages",
    "https://fedresurs.ru/api/messages",
]


def _extract_inn_fedresurs(item: dict) -> str:
    for field in ("entityInn", "inn", "companyInn", "participantInn", "debtorInn"):
        val = str(item.get(field, "")).strip()
        if re.fullmatch(r"\d{10,12}", val):
            return val
    text = " ".join(str(item.get(k, "")) for k in
                    ("messageText", "text", "title", "description", "entityName", "debtorName"))
    m = INN_RE.search(text)
    return m.group(1) if m else ""


def _fedresurs_search(keyword: str, fed_session: requests.Session) -> list[str]:
    working_ep = None
    for ep in FEDRESURS_ENDPOINTS:
        try:
            r = fed_session.get(
                ep,
                params={"searchString": keyword, "limit": 5, "offset": 0},
                headers={"User-Agent": _ua(), "Accept": "application/json",
                         "Referer": "https://fedresurs.ru/"},
                timeout=20,
            )
            log.debug(f"Федресурс probe {ep}: HTTP {r.status_code}")
            if r.status_code == 200:
                working_ep = ep
                break
        except Exception as e:
            log.debug(f"Федресурс {ep}: {e}")
            continue

    if not working_ep:
        log.warning(f"Федресурс: нет рабочего endpoint для '{keyword}'")
        _inc("fedresurs_no_endpoint")
        return []

    inns = []
    offset, limit = 0, 40

    while offset < 200:
        try:
            r = fed_session.get(
                working_ep,
                params={"searchString": keyword, "limit": limit, "offset": offset},
                headers={"User-Agent": _ua(), "Accept": "application/json",
                         "Origin": "https://fedresurs.ru", "Referer": "https://fedresurs.ru/"},
                timeout=20,
            )
            if r.status_code != 200:
                log.debug(f"Федресурс страница HTTP {r.status_code}")
                break

            data  = r.json()
            items = (data.get("data") or data.get("items") or
                     data.get("content") or data.get("messages") or [])
            total = (data.get("total") or data.get("totalElements") or
                     data.get("count") or 0)

            log.debug(f"Федресурс '{keyword}' offset={offset}: items={len(items)}, total={total}")
            if not items:
                break

            for item in items:
                inn = _extract_inn_fedresurs(item)
                if inn:
                    inns.append(inn)

            offset += limit
            if total and offset >= total:
                break
            _sleep(0.8, 1.5)
        except Exception as e:
            log.debug(f"Федресурс ошибка: {e}")
            break

    return inns


def get_inns_from_fedresurs(keywords: list | None = None) -> list[str]:
    keywords = keywords or MINING_KEYWORDS
    fed_session = requests.Session()
    seen: set[str] = set()

    log.info(f"Федресурс: поиск по {len(keywords)} ключевым словам")
    for kw in keywords:
        log.info(f"  → «{kw}»")
        found = _fedresurs_search(kw, fed_session)
        log.info(f"    ИНН найдено: {len(found)}")
        seen.update(found)
        _sleep(2.0, 3.5)

    result = list(seen)
    log.info(f"Федресурс итого: {len(result)} уникальных ИНН")
    _inc("fedresurs_inns", len(result))
    return result


# ══════════════════════════════════════════════════════════
# ГИР БО
# ══════════════════════════════════════════════════════════

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
    return data if isinstance(data, list) else data.get("content", [data])


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
            _inc("girbo_not_found")
            return None

        org    = orgs[0]
        org_id = org.get("id")
        if not org_id:
            return None

        bfo_list = _girbo_get_bfo_list(org_id)
        if not bfo_list:
            _inc("girbo_no_bfo")
            return None

        target_bfo = next(
            (b for b in bfo_list
             if str(b.get("year") or b.get("reportYear") or "") == str(year)),
            bfo_list[0],
        )

        bfo_id        = target_bfo.get("id")
        report_detail = _girbo_get_bfo_detail(bfo_id) if bfo_id else target_bfo

        _inc("girbo_ok")
        return {
            "inn":       inn,
            "org_name":  org.get("shortName") or org.get("name", ""),
            "region":    org.get("region", ""),
            "okvd_main": org.get("okved", ""),
            "report":    report_detail,
        }
    except Exception as e:
        log.debug(f"ИНН {inn}: ГИРБО ошибка — {e}")
        _inc("girbo_error")
        return None


def _parse_number(s) -> float | None:
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

    if electricity >= 50_000_000:
        score += 30; triggers.append(f"ЭЭ>50млн(+30)")
    elif electricity >= 10_000_000:
        score += 20; triggers.append(f"ЭЭ>10млн(+20)")
    elif electricity >= 5_000_000:
        score += 10; triggers.append(f"ЭЭ>5млн(+10)")
    elif electricity > 0:
        score += 5;  triggers.append(f"ЭЭ найдена(+5)")

    if rev > 0 and cost > 0:
        r = cost / rev
        if r > 0.80:
            score += 20; triggers.append(f"себ/выр>{r:.0%}(+20)")
        elif r > 0.70:
            score += 15; triggers.append(f"себ/выр>{r:.0%}(+15)")
        elif r > 0.60:
            score += 8;  triggers.append(f"себ/выр>{r:.0%}(+8)")

    if bal > 0:
        fa_r = fa / bal
        if fa_r > 0.60:
            score += 20; triggers.append(f"ОС/бал>{fa_r:.0%}(+20)")
        elif fa_r > 0.40:
            score += 12; triggers.append(f"ОС/бал>{fa_r:.0%}(+12)")

    if emp == 0 and rev > 5_000_000:
        score += 10; triggers.append("0сотр+выручка(+10)")
    elif emp > 0 and rev / emp > 20_000_000:
        score += 15; triggers.append(f"выр/сотр>20млн(+15)")
    elif emp > 0 and rev / emp > 10_000_000:
        score += 8;  triggers.append(f"выр/сотр>10млн(+8)")

    return min(score, 100), triggers


def get_priority_label(score: int) -> str:
    if score >= 70: return "Горячий"
    elif score >= 40: return "Тёплый"
    else: return "Холодный"


def load_inns_from_file(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {filepath}")
    if filepath.endswith(".csv"):
        df  = pd.read_csv(filepath, dtype=str)
        col = next((c for c in df.columns if "инн" in c.lower() or "inn" in c.lower()), df.columns[0])
        inns = df[col].str.strip().tolist()
    else:
        with open(filepath, encoding="utf-8") as f:
            inns = [l.strip() for l in f if l.strip()]
    return [i for i in inns if i.isdigit() and len(i) in (10, 12)]


# ══════════════════════════════════════════════════════════
# Основной пайплайн
# ══════════════════════════════════════════════════════════

def run_parser(
    category: str = "hosting",
    inn_source: str = "api",
    min_electricity: float = MIN_ELECTRICITY_EXPENSE,
    year: int = REPORT_YEAR,
    output: str | None = None,
    min_score: int = 20,
):
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    today  = date.today().strftime("%Y-%m-%d")
    suffix = "hosting" if category == "hosting" else "mining"

    report_path = f"{OUTPUT_DIR}/run_report_{suffix}_{today}.txt"
    report_lines = [
        f"=== Запуск {category} / {today} ===",
        f"источник: {inn_source}",
        f"год отчётности: {year}",
        f"мин.ЭЭ: {min_electricity:,.0f} руб",
        f"мин.скор: {min_score}",
        "",
    ]

    def _save_report(extra=""):
        report_lines.append(extra)
        for k, v in sorted(_stats.items()):
            report_lines.append(f"  {k}: {v}")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))
        log.info(f"Отчёт сохранён: {report_path}")

    # ── Шаг 1: ИНН ────────────────────────────────────────
    log.info(f"Шаг 1: получение ИНН (категория={category}, источник={inn_source})")
    try:
        if inn_source != "api":
            inns = load_inns_from_file(inn_source)
        elif category == "mining":
            inns = get_inns_from_fedresurs()
        else:
            inns = get_inns_by_okveds(HOSTING_OKVEDS, TARGET_REGIONS)
    except Exception as e:
        log.error(f"Ошибка получения ИНН: {e}")
        _save_report(f"ОШИБКА: {e}")
        return None

    report_lines.append(f"ИНН получено: {len(inns)}")
    log.info(f"ИНН для анализа: {len(inns)}")

    if not inns:
        log.error("ИНН не получены — нечего анализировать.")
        _save_report("РЕЗУЛЬТАТ: 0 ИНН")
        return None

    # Сохраняем список ИНН
    with open(f"{OUTPUT_DIR}/inns_{suffix}_{today}.txt", "w") as f:
        f.write("\n".join(inns))

    # ── Шаг 2: ГИРБО ─────────────────────────────────────
    log.info("Шаг 2: анализ бухотчётности через ГИР БО...")
    results = []
    girbo_found = 0
    girbo_missing = 0

    max_inns = min(len(inns), 500)  # ограничение на 1 запуск
    for inn in tqdm(inns[:max_inns], desc="Анализ"):
        time.sleep(REQUEST_DELAY)

        report_data = get_report_from_girbo(inn, year=year)
        if not report_data:
            girbo_missing += 1
            continue

        girbo_found += 1
        electricity     = extract_electricity_expenses(report_data)
        financials      = extract_key_financials(report_data)
        score, triggers = calculate_mining_score(financials, electricity)

        # Пропускаем совсем нулевые записи
        if financials["revenue"] == 0 and financials["balance_total"] == 0:
            continue

        # Фильтр хостинга
        if category == "hosting":
            proxy_monthly = financials["cost_of_sales"] * 0.40 / 12
            if (electricity < min_electricity
                    and proxy_monthly < min_electricity
                    and score < min_score):
                continue

        results.append({
            "ИНН":               inn,
            "Категория":         "Хостинг" if category == "hosting" else "Майнинг",
            "Компания":          report_data.get("org_name", ""),
            "Регион":            report_data.get("region", ""),
            "ОКВЭД":             report_data.get("okvd_main", ""),
            "Расходы_ЭЭ_руб":   int(electricity),
            "Прокси_ЭЭ_мес":    int(financials["cost_of_sales"] * 0.40 / 12),
            "Выручка_руб":       int(financials["revenue"]),
            "Себестоимость_руб": int(financials["cost_of_sales"]),
            "ОС_руб":            int(financials["fixed_assets"]),
            "Баланс_руб":        int(financials["balance_total"]),
            "Скоринг":           score,
            "Приоритет":         get_priority_label(score),
            "Триггеры":          " | ".join(triggers),
        })

    report_lines += [
        f"ГИРБО найдено: {girbo_found}",
        f"ГИРБО не найдено: {girbo_missing}",
        f"Прошли фильтры: {len(results)}",
    ]

    log.info(f"ГИРБО: найдено={girbo_found}, нет={girbo_missing}, в результате={len(results)}")

    if not results:
        log.warning("Компаний не найдено по критериям. Смотри run_report.")
        _save_report("РЕЗУЛЬТАТ: 0 компаний прошли фильтры")
        return None

    df = pd.DataFrame(results)
    df = df.sort_values("Скоринг", ascending=False).reset_index(drop=True)

    out = output or f"{OUTPUT_DIR}/mining_leads_{suffix}_{today}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")

    hot  = len(df[df["Приоритет"] == "Горячий"])
    warm = len(df[df["Приоритет"] == "Тёплый"])

    cat_label = "ХОСТИНГ / ЦОД" if category == "hosting" else "МАЙНЕРЫ"
    log.info("=" * 55)
    log.info(f"  {cat_label} — найдено: {len(df)}")
    log.info(f"  Горячих: {hot} | Тёплых: {warm}")
    log.info(f"  CSV: {out}")
    log.info("=" * 55)

    _save_report(f"РЕЗУЛЬТАТ: {len(df)} компаний")
    return df


def parse_args():
    parser = argparse.ArgumentParser(description="Парсер майнинговых компаний и хостингов")
    parser.add_argument("--category", choices=["hosting", "mining"], default="hosting")
    parser.add_argument("--source", default="api",
        help='"api" — ЕГРЮЛ/Федресурс, или путь к файлу inns.txt')
    parser.add_argument("--min-ee", type=float, default=MIN_ELECTRICITY_EXPENSE,
        dest="min_electricity")
    parser.add_argument("--year", type=int, default=REPORT_YEAR)
    parser.add_argument("--output", default=None)
    parser.add_argument("--min-score", type=int, default=20, dest="min_score")
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
