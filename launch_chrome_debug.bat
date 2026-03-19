@echo off
echo Запускаем Chrome с отладочным портом 9222...
echo После запуска откроется Chrome. Перейди на fedresurs.ru вручную.
echo Затем запускай main.py - он подключится к этому Chrome.
echo.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome_debug_fedresurs"
timeout /t 3 /nobreak >nul
echo Chrome запущен. Теперь в Python терминале запускай:
echo   python main.py --mode fedresurs --skip-db --debug
pause
