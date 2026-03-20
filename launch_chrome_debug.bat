@echo off
echo ============================================================
echo  Запуск Chrome с реальным профилем + отладочный порт 9222
echo ============================================================
echo.
echo [1] Закрываем существующий Chrome (если открыт)...
taskkill /F /IM chrome.exe /T 2>nul
timeout /t 2 /nobreak >nul

echo [2] Запускаем Chrome с реальным профилем и портом 9222...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --remote-debugging-port=9222 ^
    --no-first-run ^
    --no-default-browser-check ^
    "https://fedresurs.ru/"

echo.
echo [3] Ждём загрузки Chrome...
timeout /t 8 /nobreak >nul

echo.
echo ============================================================
echo  Chrome запущен с РЕАЛЬНЫМ профилем (есть Qrator-cookies)
echo  fedresurs.ru загружается...
echo.
echo  Подожди пока страница полностью загрузится (10-15 сек),
echo  затем запускай в Python-терминале:
echo.
echo    python main.py --mode fedresurs --skip-db --debug
echo ============================================================
echo.
pause
