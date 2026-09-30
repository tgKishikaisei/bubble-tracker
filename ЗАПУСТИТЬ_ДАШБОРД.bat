@echo off
chcp 65001 >nul
title Bubble Tracker PRO — Dashboard

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║       BUBBLE TRACKER PRO v5.0                  ║
echo  ║       Unified Dashboard + Video Stream          ║
echo  ╚══════════════════════════════════════════════════╝
echo.
cd /d "%~dp0"

:: ─── 1. Запуск React UI (дашборд) ────────────────────────────────
:: API теперь запускается ВНУТРИ bubble_tracker_pro.py автоматически.
:: Этот батник запускает только фронтенд-дашборд.
echo  [1/2] Запускаю дашборд (порт 5173)...
start "Bubble Dashboard UI" cmd /k "chcp 65001 >nul && cd /d "%~dp0bubble-dashboard-ui" && npm run dev"

:: Ждём пока Vite поднимется
timeout /t 4 /nobreak >nul

:: ─── 2. Запуск основной программы ───────────────────────────────
:: bubble_tracker_pro.py сам запустит API (порт 8000) и откроет браузер.
echo  [2/2] Запускаю Bubble Tracker PRO...
start "Bubble Tracker PRO" cmd /k "chcp 65001 >nul && cd /d "%~dp0" && python bubble_tracker_pro.py"

echo.
echo  ✅ Готово! Программа запущена.
echo.
echo  Порядок действий:
echo    1. В появившемся диалоге выберите видео или камеру
echo    2. Дашборд откроется автоматически в браузере
echo    3. Видеопоток появится в дашборде (LIVE панель вверху)
echo.
echo  Дашборд:      http://localhost:5173
echo  API:          http://localhost:8000
echo  Видео стрим:  http://localhost:8000/api/stream
echo.
pause
