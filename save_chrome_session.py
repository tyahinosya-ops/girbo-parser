"""
save_chrome_session.py — сохраняет сессию Федресурса из Chrome cURL.

Инструкция:
  1. Открой Chrome, зайди на fedresurs.ru
  2. Открой DevTools (F12) → вкладка Network
  3. В поиске на сайте введи "antminer" → дождись результатов
  4. В Network найди запрос к /backend/encumbrances?searchString=antminer...
  5. ПКМ по запросу → Copy → Copy as cURL (bash)
  6. Запусти: python save_chrome_session.py
  7. Вставь скопированный cURL и нажми Enter дважды (пустая строка = конец)

Сессия сохраняется в chrome_session.json и используется main.py автоматически.
"""
import json
import re
import sys
from pathlib import Path

OUTPUT = Path("chrome_session.json")


def parse_curl(curl_text: str) -> dict:
    headers = {}
    cookies = {}

    # Извлекаем заголовки (-H 'Name: Value')
    for m in re.finditer(r"-H\s+'([^:]+):\s*([^']+)'", curl_text):
        name, value = m.group(1).strip(), m.group(2).strip()
        if name.lower() == "cookie":
            # Парсим строку cookies
            for part in value.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        else:
            headers[name] = value

    # Альтернативный формат --header
    for m in re.finditer(r'--header\s+"([^:]+):\s*([^"]+)"', curl_text):
        name, value = m.group(1).strip(), m.group(2).strip()
        if name.lower() != "cookie":
            headers[name] = value

    # Извлекаем URL
    url_m = re.search(r"curl\s+'([^']+)'", curl_text)
    if not url_m:
        url_m = re.search(r'curl\s+"([^"]+)"', curl_text)
    url = url_m.group(1) if url_m else ""

    return {"url": url, "headers": headers, "cookies": cookies}


def main():
    print("=" * 60)
    print("Вставь cURL из Chrome DevTools:")
    print("(нажми Enter дважды для завершения ввода)")
    print("=" * 60)

    lines = []
    try:
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
    except EOFError:
        pass

    curl_text = "\n".join(lines)

    if "curl" not in curl_text.lower():
        print("\nОШИБКА: Не похоже на cURL. Скопируй через ПКМ → Copy as cURL (bash).")
        sys.exit(1)

    session = parse_curl(curl_text)

    if not session["cookies"]:
        print("\nПРЕДУПРЕЖДЕНИЕ: Cookies не найдены в cURL.")
    else:
        xsrf = session["cookies"].get("XSRF-TOKEN", "нет")
        print(f"\nНайдено cookies: {len(session['cookies'])} шт.")
        print(f"XSRF-TOKEN: {'есть (' + xsrf[:20] + '...)' if xsrf != 'нет' else 'нет'}")

    OUTPUT.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ Сессия сохранена в {OUTPUT}")
    print("  Теперь запускай: python main.py --mode fedresurs --skip-db --debug")


if __name__ == "__main__":
    main()
