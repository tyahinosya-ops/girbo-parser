"""
test_fedresurs.py — быстрая проверка доступа к API Федресурса.
Запускай из папки проекта: python test_fedresurs.py
"""
import asyncio
import httpx

BASE = "https://fedresurs.ru"

HEADERS_CHROME = {
    "User-Agent":           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept":               "application/json, text/plain, */*",
    "Accept-Language":      "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer":              "https://fedresurs.ru/encumbrances",
    "Origin":               "https://fedresurs.ru",
    "sec-ch-ua":            '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile":     "?0",
    "sec-ch-ua-platform":   '"Windows"',
    "Sec-Fetch-Site":       "same-origin",
    "Sec-Fetch-Mode":       "cors",
    "Sec-Fetch-Dest":       "empty",
    "Cache-Control":        "no-cache",
    "Pragma":               "no-cache",
}


async def test_one(label: str, url: str, headers: dict, **kwargs):
    try:
        async with httpx.AsyncClient(
            http2=False,          # HTTP/1.1 как curl
            follow_redirects=True,
            timeout=15,
        ) as client:
            r = await client.get(url, headers=headers, **kwargs)
            body_preview = r.text[:200] if r.text else "(пусто)"
            print(f"[{label}] HTTP {r.status_code} | {body_preview}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    found = data.get("found", "?")
                    page = data.get("pageData", [])
                    print(f"  → found={found}, pageData={len(page)} записей")
                except Exception:
                    pass
    except Exception as e:
        print(f"[{label}] ОШИБКА: {e}")


async def main():
    import json

    print("=" * 60)
    print("Проверяем доступ к API Федресурса")
    print("=" * 60)

    async with httpx.AsyncClient(http2=False, follow_redirects=True, timeout=15) as client:
        r = await client.get(
            BASE + "/backend/encumbrances",
            params={"searchString": "antminer", "limit": 3, "offset": 0},
            headers=HEADERS_CHROME,
        )
        print(f"HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"found={data.get('found')}, pageData={len(data.get('pageData', []))}")
            print()
            print("=== ПОЛНАЯ СТРУКТУРА ПЕРВОЙ ЗАПИСИ ===")
            if data.get("pageData"):
                print(json.dumps(data["pageData"][0], indent=2, ensure_ascii=False))
        else:
            print(r.text[:500])

asyncio.run(main())
