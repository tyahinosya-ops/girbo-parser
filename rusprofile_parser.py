"""
rusprofile_parser.py — выгрузка ИНН с rusprofile.ru
=====================================================
Фильтрует компании по:
- ОКВЭД (майнинг и смежные)
- Региону
- Основным средствам (если доступно)
Требования:
    pip install requests pandas beautifulsoup4 lxml tqdm fake-useragent
"""
import requests
import pandas as pd
import time
import logging
import random
import re
from bs4 import BeautifulSoup
from tqdm import tqdm
from pathlib import Path
from datetime import date
logging.basicConfig(
    level=logging.INFO,
    format="%(H:%M:%S) [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────
# ОКВЭД для поиска (rusprofile принимает без точки)
OKVEDS = [
    "63.11",
    "62.09",
    "62.01",
    "64.19",
    "66.19",
    "35.11",
    "35.14",
    "46.51",
    "46.52",
]
# Регионы rusprofile (slug из URL)
REGIONS = [
    "irkutskaya-oblast",
    "krasnoyarskiy-kray",
    "respublika-hakasiya",
    "respublika-buryatiya",
    "respublika-kareliya",
    "kabardino-balkarskaya-respublika",
    "respublika-dagestan",
]
# Задержка между запросами (сек) — важно, иначе бан
MIN_DELAY = 1.5
MAX_DELAY = 3.5
OUTPUT_FILE = f"rusprofile_inns_{date.today()}.csv"
# Минимальная выручка (фильтр на странице компании)
MIN_REVENUE = 1_000_000
# ─────────────────────────────────────────────
# ЗАГОЛОВКИ — имитируем браузер
# ─────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]
def get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.rusprofile.ru/",
    }
def random_delay():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
# ─────────────────────────────────────────────
# ПАРСИНГ СПИСКА КОМПАНИЙ
# ─────────────────────────────────────────────
def build_search_url(okvd: str, region: str, page: int = 1) -> str:
    """
    Формирует URL поиска на rusprofile.ru.
    
    Пример: https://www.rusprofile.ru/search?query=&okved=63.11&region=irkutskaya-oblast&page=1
    """
    okvd_clean = okvd.replace(".", "")  # rusprofile без точки
    return (
        f"https://www.rusprofile.ru/codes/{okvd_clean}"
        f"?region={region}&page={page}"
    )
def parse_company_list_page(html: str) -> list[dict]:
    """
    Парсит страницу со списком компаний.
    Возвращает список словарей с базовыми данными.
    """
    soup = BeautifulSoup(html, "lxml")
    companies = []
    # Карточки компаний на rusprofile
    cards = soup.select("div.company-item")
    
    if not cards:
        # Альтернативный селектор
        cards = soup.select("article.company-item, div.search-result-item")
    for card in cards:
        try:
            company = {}
            # Название
            name_el = card.select_one("a.company-name, .org-name a, h3 a")
            if name_el:
                company["name"] = name_el.get_text(strip=True)
                company["url"] = "https://www.rusprofile.ru" + name_el.get("href", "")
            # ИНН — часто есть прямо в карточке
            inn_el = card.select_one(".company-inn, [data-inn]")
            if inn_el:
                inn_text = inn_el.get_text(strip=True)
                inn_match = re.search(r"\d{10,12}", inn_text)
                if inn_match:
                    company["inn"] = inn_match.group()
            
            # ИНН из data-атрибута
            if "inn" not in company:
                inn_data = card.get("data-inn") or card.select_one("[data-inn]")
                if inn_data:
                    company["inn"] = str(inn_data).strip()
            # ИНН из URL компании (формат /id/1234567890)
            if "inn" not in company and "url" in company:
                url_inn = re.search(r"/id/(\d{10,12})", company["url"])
                if url_inn:
                    company["inn"] = url_inn.group(1)
            # Регион
            region_el = card.select_one(".company-region, .org-address")
            if region_el:
                company["region"] = region_el.get_text(strip=True)
            # ОКВЭД
            okvd_el = card.select_one(".company-okved, .okved")
            if okvd_el:
                company["okvd"] = okvd_el.get_text(strip=True)
            # Статус (действующая / ликвидирована)
            status_el = card.select_one(".company-status, .status")
            company["status"] = status_el.get_text(strip=True) if status_el else "неизвестно"
            if company.get("inn") or company.get("name"):
                companies.append(company)
        except Exception as e:
            log.debug(f"Ошибка парсинга карточки: {e}")
            continue
    return companies
def get_total_pages(html: str) -> int:
    """Определяет общее количество страниц в результатах."""
    soup = BeautifulSoup(html, "lxml")
    
    # Ищем пагинацию
    pagination = soup.select("a.pagination-item, .pager a, nav.pagination a")
    
    if not pagination:
        return 1
    
    max_page = 1
    for el in pagination:
        text = el.get_text(strip=True)
        try:
            page_num = int(text)
            max_page = max(max_page, page_num)
        except ValueError:
            continue
    
    return min(max_page, 50)  # Лимит 50 страниц на запрос
# ─────────────────────────────────────────────
# ПАРСИНГ СТРАНИЦЫ КОМПАНИИ (детали)
# ─────────────────────────────────────────────
def parse_company_details(url: str, session: requests.Session) -> dict:
    """
    Загружает страницу компании и извлекает финансовые данные.
    Используется для доп. фильтрации по ОС и выручке.
    """
    details = {
        "inn": "",
        "ogrn": "",
        "revenue": 0,
        "fixed_assets": 0,
        "director": "",
        "registered": "",
        "employees": 0,
        "authorized_capital": 0,
    }
    try:
        random_delay()
        resp = session.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200:
            return details
        soup = BeautifulSoup(resp.text, "lxml")
        # ИНН
        inn_el = soup.select_one("[itemprop='taxID'], .requisite-item:contains('ИНН') + .requisite-value")
        if inn_el:
            inn_match = re.search(r"\d{10,12}", inn_el.get_text())
            if inn_match:
                details["inn"] = inn_match.group()
        # ОГРН
        ogrn_el = soup.select_one("[itemprop='identifier']")
        if ogrn_el:
            details["ogrn"] = ogrn_el.get_text(strip=True)
        # Финансы — rusprofile показывает выручку и прибыль
        finance_rows = soup.select("table.finance-table tr, .finances-row")
        for row in finance_rows:
            text = row.get_text(" ", strip=True).lower()
            
            # Выручка
            if "выручка" in text:
                amounts = re.findall(r"[\d\s]+(?:тыс|млн|млрд)?", text)
                for a in amounts:
                    try:
                        val = float(a.replace(" ", "").replace("\xa0", ""))
                        if val > 0:
                            details["revenue"] = val * 1000  # тыс. → руб.
                            break
                    except:
                        continue
            # Основные средства
            if "основные средства" in text or "внеоборотные" in text:
                amounts = re.findall(r"[\d\s]+", text)
                for a in amounts:
                    try:
                        val = float(a.replace(" ", "").replace("\xa0", ""))
                        if val > 0:
                            details["fixed_assets"] = val * 1000
                            break
                    except:
                        continue
        # Руководитель
        director_el = soup.select_one(".director-name, [itemprop='name']")
        if director_el:
            details["director"] = director_el.get_text(strip=True)
    except Exception as e:
        log.debug(f"Ошибка деталей {url}: {e}")
    return details
# ─────────────────────────────────────────────
# ОСНОВНОЙ ПАРСЕР
# ─────────────────────────────────────────────
def run_rusprofile_parser(
    okveds: list = OKVEDS,
    regions: list = REGIONS,
    output: str = OUTPUT_FILE,
    fetch_details: bool = False,  # True = медленнее, но с финансами
    min_revenue: float = MIN_REVENUE,
):
    session = requests.Session()
    all_companies = []
    seen_inns = set()
    log.info(f"Старт: {len(okveds)} ОКВЭД × {len(regions)} регионов")
    for okvd in okveds:
        for region in regions:
            log.info(f"→ ОКВЭД {okvd} / {region}")
            # Первая страница
            url = build_search_url(okvd, region, page=1)
            try:
                random_delay()
                resp = session.get(url, headers=get_headers(), timeout=15)
                
                if resp.status_code == 429:
                    log.warning("Бан (429) — ждём 30 сек...")
                    time.sleep(30)
                    continue
                    
                if resp.status_code != 200:
                    log.warning(f"HTTP {resp.status_code}: {url}")
                    continue
                total_pages = get_total_pages(resp.text)
                companies = parse_company_list_page(resp.text)
                log.info(f"  Страниц: {total_pages}, компаний на стр.1: {len(companies)}")
                # Добавляем с первой страницы
                for c in companies:
                    inn = c.get("inn", "")
                    if inn and inn not in seen_inns:
                        seen_inns.add(inn)
                        c["okvd_search"] = okvd
                        c["region_search"] = region
                        all_companies.append(c)
                # Остальные страницы
                for page in range(2, total_pages + 1):
                    random_delay()
                    page_url = build_search_url(okvd, region, page=page)
                    
                    try:
                        page_resp = session.get(page_url, headers=get_headers(), timeout=15)
                        if page_resp.status_code != 200:
                            break
                            
                        page_companies = parse_company_list_page(page_resp.text)
                        
                        if not page_companies:
                            break  # Пустая страница — конец
                            
                        for c in page_companies:
                            inn = c.get("inn", "")
                            if inn and inn not in seen_inns:
                                seen_inns.add(inn)
                                c["okvd_search"] = okvd
                                c["region_search"] = region
                                all_companies.append(c)
                    except Exception as e:
                        log.warning(f"Ошибка стр. {page}: {e}")
                        break
            except Exception as e:
                log.warning(f"Ошибка: {e}")
                continue
    log.info(f"\nСобрано уникальных компаний: {len(all_companies)}")
    if not all_companies:
        log.error("Ничего не найдено. Проверьте интернет-соединение или селекторы.")
        return
    # Опционально — загрузка деталей (финансы, ОС)
    if fetch_details:
        log.info("Загрузка финансовых данных (медленный режим)...")
        
        for company in tqdm(all_companies, desc="Детали"):
            if not company.get("url"):
                continue
            details = parse_company_details(company["url"], session)
            company.update({
                "inn": company.get("inn") or details.get("inn", ""),
                "ogrn": details.get("ogrn", ""),
                "revenue_rub": details.get("revenue", 0),
                "fixed_assets_rub": details.get("fixed_assets", 0),
                "director": details.get("director", ""),
            })
        # Фильтрация по выручке
        if min_revenue > 0:
            before = len(all_companies)
            all_companies = [
                c for c in all_companies
                if c.get("revenue_rub", 0) >= min_revenue
                or c.get("revenue_rub", 0) == 0  # Оставляем если данных нет
            ]
            log.info(f"После фильтра выручки: {len(all_companies)} (убрано: {before - len(all_companies)})")
    # Сохранение
    df = pd.DataFrame(all_companies)
    # Чистим — убираем ликвидированные
    if "status" in df.columns:
        df = df[~df["status"].str.lower().str.contains("ликвид|закрыт|недейств", na=False)]
    # Убираем дубли по ИНН
    if "inn" in df.columns:
        df = df.drop_duplicates(subset=["inn"])
    # Сортируем
    if "fixed_assets_rub" in df.columns:
        df = df.sort_values("fixed_assets_rub", ascending=False)
    Path("output").mkdir(exist_ok=True)
    out_path = f"output/{output}"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    # Отдельно — только ИНН для скармливания в girbo_parser
    inn_only = df[df["inn"].notna() & (df["inn"] != "")]["inn"]
    inn_path = f"output/inns_{date.today()}.txt"
    inn_only.to_csv(inn_path, index=False, header=False)
    print("\n" + "="*55)
    print(f"  ✅ Готово!")
    print(f"  📋 Компаний найдено:  {len(df)}")
    print(f"  🔑 ИНН извлечено:     {inn_only.notna().sum()}")
    print(f"  📁 Полный CSV:        {out_path}")
    print(f"  📁 Только ИНН:        {inn_path}")
    print("="*55)
    print("\n  ➡️  Следующий шаг:")
    print(f"  python main.py --mode file --input {inn_path}")
    print()
    return df
# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Rusprofile Parser — выгрузка ИНН майнинговых компаний"
    )
    parser.add_argument(
        "--details",
        action="store_true",
        default=False,
        help="Загружать финансы с каждой страницы компании (медленно, зато с ОС и выручкой)",
    )
    parser.add_argument(
        "--min-revenue",
        type=float,
        default=1_000_000,
        dest="min_revenue",
        help="Минимальная выручка в рублях (работает только с --details)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=OUTPUT_FILE,
        help="Имя выходного файла",
    )
    args = parser.parse_args()
    run_rusprofile_parser(
        fetch_details=args.details,
        min_revenue=args.min_revenue,
        output=args.output,
    )
