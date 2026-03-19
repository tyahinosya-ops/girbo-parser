"""
Диагностика: перехватываем ВСЕ JSON-запросы Федресурса при поиске 'antminer'.
Показывает реальный backend-URL поисковой выдачи.
"""
import asyncio
import json
import logging
import random

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s – %(levelname)s – %(message)s")
log = logging.getLogger("intercept")

BASE = "https://fedresurs.ru"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,   # видим что происходит
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 900},
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        all_json_calls = []

        async def on_response(response):
            url = response.url
            if "fedresurs.ru" not in url:
                return
            if any(s in url for s in [".js", ".css", ".png", ".woff", ".ico", "mc.yandex", "favicon"]):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                keys = list(body.keys()) if isinstance(body, dict) else f"list[{len(body)}]"
                preview = json.dumps(body, ensure_ascii=False)[:300]
                from urllib.parse import urlparse
                path = urlparse(url).path
                entry = {"url": url, "path": path, "keys": keys, "preview": preview}
                all_json_calls.append(entry)
                print(f"\n{'='*60}")
                print(f"JSON: {url}")
                print(f"Keys: {keys}")
                print(f"Preview: {preview[:200]}")
            except Exception as e:
                print(f"  ошибка разбора {url}: {e}")

        page.on("response", on_response)

        # Шаг 1: Открываем главную (Qrator challenge)
        print("[1] Загружаем главную страницу...")
        await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(6)
        print(f"    URL после редиректов: {page.url}")

        # Шаг 2: Навигация на поиск antminer
        print("\n[2] Открываем поиск по 'antminer'...")
        search_url = f"{BASE}/encumbrances?searchString=antminer&group=all&period=%7B%7D&additionalFnpSearch=true&limit=15&offset=0"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)

        print("    Ждём 15 секунд (SPA грузит данные)...")
        await asyncio.sleep(15)

        print(f"\n{'='*60}")
        print(f"ИТОГО JSON-вызовов: {len(all_json_calls)}")
        for i, c in enumerate(all_json_calls):
            print(f"  [{i+1}] {c['path']}")
            print(f"       keys: {c['keys']}")

        # Сохраняем для анализа
        with open("output/intercept_log.json", "w", encoding="utf-8") as f:
            json.dump(all_json_calls, f, ensure_ascii=False, indent=2)
        print("\nЛог сохранён: output/intercept_log.json")

        input("\nНажми Enter для закрытия браузера...")
        await browser.close()

asyncio.run(main())
