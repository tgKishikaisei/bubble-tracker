"""

   BUBBLE TRACKER PRO  v5.0  — Counting Line Edition                
                                                                      
   АРХИТЕКТУРА (Senior-grade):                                       
   • Counting Line — пузырёк считается РОВНО 1 РАЗ при пересечении  
     горизонтальной линии снизу вверх. Двойной счёт физически      
     невозможен.                                                      
   • Velocity Prediction — предсказание следующей позиции по         
     скорости → устойчивое слежение в плотном потоке.               
   • Exit Zones — место подсчитанного пузырька блокируется.          
   • 6-Stage Detection Pipeline (CLAHE, TopHat, MOG2,               
     HoughCircles, Contours, BlobDetector).                          
   • Frame-by-frame stepping, speed control 0.25×–2×.               

"""

import os
# Подавляем предупреждения OpenCV (MSMF/HEVC WARN-сообщения — не ошибки)
os.environ.setdefault("OPENCV_LOG_LEVEL",         "ERROR")   # скрыть WARN
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL",   "8")       # AV_LOG_FATAL
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]      = "threads;1"

import cv2
import numpy as np
import json
import csv
import math
import time
from datetime import datetime
from pathlib import Path
from collections import deque
import threading

# ─── авто-установка зависимостей ────────────────────────────────────────────
def _ensure(pkg, alias=None):
    import importlib, subprocess, sys
    try: importlib.import_module(alias or pkg)
    except ImportError:
        print(f"[SETUP] pip install {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

_ensure("openpyxl")
_ensure("scipy")
_ensure("scikit-image", "skimage")
_ensure("uvicorn")
_ensure("fastapi")

import sys
import socket

# ─── Запуск API сервера в фоновом потоке ─────────────────────────────────────
def _is_port_free(port: int) -> bool:
    """Проверяет свободен ли порт — если уже занят, не запускаем второй uvicorn."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) != 0

def start_api_server(data_dir: str = None, port: int = 8000):
    """
    Запускает FastAPI (bubble-api/main.py) в daemon-потоке.
    
    Почему daemon thread?
    При закрытии основной программы поток автоматически убивается.
    uvicorn.run() блокирует поток — поэтому он в отдельном потоке.
    
    data_dir — путь к bubble_results/ (передаём через env переменную).
    """
    if not _is_port_free(port):
        print(f"[API] Порт {port} уже занят — API уже запущен, пропускаем.")
        return
    
    # Устанавливаем DATA_DIR чтобы API знал где искать sessions.json
    if data_dir:
        os.environ["DATA_DIR"] = data_dir
    
    def _run():
        import uvicorn
        # Добавляем bubble-api в sys.path чтобы найти main.py
        api_dir = str(Path(__file__).parent / "bubble-api")
        if api_dir not in sys.path:
            sys.path.insert(0, api_dir)
        try:
            uvicorn.run(
                "main:app",
                # По умолчанию 127.0.0.1: видеопоток и команды не видны локальной сети
                host=os.environ.get("BUBBLE_API_HOST", "127.0.0.1"),
                port=port,
                log_level="warning",  # минимум логов чтобы не засорять консоль
                reload=False,
            )
        except Exception as ex:
            print(f"[API ERROR] {ex}")
    
    t = threading.Thread(target=_run, daemon=True, name="BubbleAPI")
    t.start()
    print(f"[API] Запускается на http://localhost:{port}")
    # Небольшая задержка чтобы API успел подняться
    time.sleep(2.0)

def _open_browser(url: str):
    """Открывает браузер в фоне, не блокируя главный поток."""
    import webbrowser
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ── scipy/skimage: ленивый импорт на уровне модуля ─────────────────────
try:
    from scipy.ndimage import distance_transform_edt as _dt_edt
    from skimage.feature import peak_local_max as _peak_local_max
    from skimage.segmentation import watershed as _sk_watershed
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("[WARN] scipy/scikit-image не доступны, Watershed отключен")

# ── Классификация формы пузырька по соотношению полуосей ─────────────────
def _classify_shape(a_min: float, a_max: float) -> str:
    """Возвращает 'circle', 'ellipse' или 'irregular' по соотношению полуосей.
    
    Пороги:
      a_min/a_max > 0.85 → круглый  (незначительное отклонение)
      a_min/a_max > 0.50 → эллипс   (заметное вытягивание)
      иначе              → нет определённой формы
    """
    if a_max <= 0:
        return "circle"
    ratio = a_min / a_max
    if ratio > 0.85:
        return "circle"
    elif ratio > 0.50:
        return "ellipse"
    else:
        return "irregular"


class BubbleTracker:
    """
    Трекер на основе пересечения счётной линии.

    Алгоритм:
      1. Детектируем пузырьки на кадре.
      2. IoU-дедупликация: убираем дубли внутри одного кадра.
      3. Exit-zone фильтр: игнорируем детекции в зонах недавно
         подсчитанных пузырьков.
      4. Мэтчинг по предсказанным позициям (velocity extrapolation).
         Это главное улучшение против ID-switching в плотном потоке.
      5. Velocity update: экспоненциальное скользящее среднее.
      6. Crossing check: если трек перешёл через counting_line_y
         снизу вверх — COUNT +1 → add to counted set → exit zone.

    ДВОЙНОЙ СЧЁТ НЕВОЗМОЖЕН:
      - counted: set — ID туда попадает и никогда не уходит.
      - exit zone блокирует место на exit_memory кадров.
    """

    def __init__(self, max_disappeared=10, max_distance=80,
                 iou_threshold=0.20, exit_memory=30,
                 min_track_frames=3, velocity_alpha=0.45, px_per_mm=10.0, fps=25.0,
                 enable_per_frame_log=True):

        self.next_id          = 1
        self.frame_n          = 0

        # трек-данные
        self.positions:   dict = {}   # id -> (x, y)
        self.velocities:  dict = {}   # id -> (vx, vy)   скользящая скорость
        self.radii:       dict = {}   # id -> deque[r]
        self.disappeared: dict = {}   # id -> кол-во пропущенных кадров
        self.prev_y:      dict = {}   # id -> y прошлого кадра (для crossing)
        self.age:         dict = {}   # id -> кол-во кадров трека
        self.x_history:   dict = {}
        self.y_history:   dict = {}   # id -> deque[y] для скользящего окна скорости
        self.intensities: dict = {}
        self.ellipses:    dict = {}   # id -> deque[(a_min, a_max, angle)]
        self.enable_per_frame_log = enable_per_frame_log
        self.per_frame_log: list = []  # покадровый лог ВСЕХ пузырьков (каждый кадр)

        # счёт
        self.total:   int = 0         # всего подсчитанных пузырьков
        self.counted: set = set()     # ID уже учтённых (никогда не сбрасывается)
        self.detailed_log: list = []  # логи данных пузырьков

        # exit-зоны: [(cx, cy, r, expire_frame), ...]
        self._exit_zones: list = []

        # параметры
        self.max_disappeared  = max_disappeared
        self.max_distance     = max_distance
        self.iou_threshold    = iou_threshold
        self.exit_memory      = exit_memory
        self.min_track_frames = min_track_frames  # мин. кадров до подсчёта
        self.velocity_alpha   = velocity_alpha    # коэф. EMA скорости
        self.px_per_mm        = px_per_mm
        self.fps              = fps
        # Умное окно усреднения скорости (по рекомендации из ТЗ п.7):
        #   ≤60 FPS:  7 кадров  (за 5 кадров пузырёк пролетает 20–60 px, погрешность 1–5%)
        #   ≤120 FPS: 15 кадров (перемещение между соседними кадрами слишком мало)
        #   >120 FPS: 25 кадров (240 FPS — плотный поток, нужно больше точек)
        if fps <= 60:
            self.window_size = 7
        elif fps <= 120:
            self.window_size = 15
        else:
            self.window_size = 25
        self.px2m             = 1.0 / (px_per_mm * 1000.0) if px_per_mm > 0 else 0.0001

    # ── IoU двух кругов ───────────────────────────────────────────────
    @staticmethod
    def _ciou(c1, r1, c2, r2) -> float:
        d = math.hypot(c1[0]-c2[0], c1[1]-c2[1])
        if d >= r1+r2: return 0.0
        if d+min(r1,r2) <= max(r1,r2):
            s = min(r1,r2); return s*s / max(r1,r2)**2
        ca = max(-1., min(1., (d*d+r1*r1-r2*r2)/(2*d*r1+1e-9)))
        cb = max(-1., min(1., (d*d+r2*r2-r1*r1)/(2*d*r2+1e-9)))
        a, b = math.acos(ca), math.acos(cb)
        area  = r1*r1*(a-math.sin(2*a)/2)+r2*r2*(b-math.sin(2*b)/2)
        union = math.pi*(r1*r1+r2*r2)-area
        return area/max(union,1e-9)

    # ── IoU-дедупликация внутри кадра ─────────────────────────────────
    def _dedup(self, dets: list) -> list:
        keep, supp = [], set()
        for i, d1 in enumerate(dets):
            if i in supp: continue
            keep.append(d1)
            for j, d2 in enumerate(dets):
                if j<=i or j in supp: continue
                iou = self._ciou((d1[0],d1[1]),d1[2], (d2[0],d2[1]),d2[2])
                dist = math.hypot(d1[0]-d2[0], d1[1]-d2[1])
                # Если пересечение больше порога ИЛИ центры ближе радиуса большего (один внутри другого)
                if iou > self.iou_threshold or dist <= max(d1[2], d2[2]) * 0.9:
                    supp.add(j)
        return keep

    # ── проверка exit-зоны ────────────────────────────────────────────
    def _in_exit_zone(self, x, y, r) -> bool:
        self._exit_zones = [z for z in self._exit_zones if z[3]>self.frame_n]
        for zx, zy, zr, _ in self._exit_zones:
            if math.hypot(x-zx, y-zy) < (r+zr)*0.8:
                return True
        return False

    # ── предсказание позиции ──────────────────────────────────────────
    def _predict(self, oid) -> tuple:
        x, y = self.positions[oid]
        vx, vy = self.velocities.get(oid, (0, 0))
        return x+vx, y+vy

    # ── регистрация / дерегистрация ───────────────────────────────────
    def _reg(self, x, y, r, a_min=None, a_max=None, angle=0.0):
        nid = self.next_id
        self.positions[nid]   = (x, y)
        self.velocities[nid]  = (0.0, 0.0)
        self.radii[nid]       = deque([r], maxlen=20)
        self.ellipses[nid]    = deque([(a_min or r, a_max or r, angle)], maxlen=20)
        self.disappeared[nid] = 0
        self.prev_y[nid]      = y
        self.age[nid]         = 1
        self.x_history[nid]   = [x]
        self.y_history[nid]   = deque([y], maxlen=self.window_size)
        self.intensities[nid] = []
        self.next_id += 1

    def _dereg(self, oid):
        del self.positions[oid]
        del self.disappeared[oid]
        if oid in self.x_history: del self.x_history[oid]
        if oid in self.y_history: del self.y_history[oid]
        if oid in self.intensities: del self.intensities[oid]
        if oid in self.ellipses: del self.ellipses[oid]

    # ── главный метод ─────────────────────────────────────────────────
    def update(self, raw: list, counting_line_y: int, gray_frame=None) -> dict:
        self.frame_n += 1

        # 1. Деdup + exit-zone фильтр
        dets = self._dedup(raw)
        dets = [d for d in dets if not self._in_exit_zone(d[0], d[1], d[2])]

        # 2. Если нет детекций — увеличиваем disappeared
        if not dets:
            for oid in list(self.positions):
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self._dereg(oid)
            return self.positions

        # 3. Первые детекции — просто регистрируем
        if not self.positions:
            for d in dets: self._reg(*d)
            return self.positions

        # 4. Мэтчинг по ПРЕДСКАЗАННЫМ позициям
        used_d, used_o = set(), set()
        for oid in list(self.positions):
            px, py = self._predict(oid)           # предсказанная позиция
            bd, bi = float('inf'), -1
            for i, d in enumerate(dets):
                if i in used_d: continue
                dx, dy = d[0], d[1]
                dist = math.hypot(px-dx, py-dy)
                if dist < bd: bd, bi = dist, i

            if bi != -1 and bd < self.max_distance:
                nx, ny, nr = dets[bi][0], dets[bi][1], dets[bi][2]
                n_amin, n_amax, n_ang = dets[bi][3], dets[bi][4], dets[bi][5]
                ox, oy = self.positions[oid]

                # EMA velocity update
                a = self.velocity_alpha
                ovx, ovy = self.velocities.get(oid, (0,0))
                self.velocities[oid] = (a*(nx-ox)+(1-a)*ovx,
                                        a*(ny-oy)+(1-a)*ovy)

                self.prev_y[oid]         = oy          # сохраняем Y до обновления
                self.positions[oid]      = (nx, ny)
                self.radii[oid].append(nr)
                self.ellipses[oid].append((n_amin, n_amax, n_ang))
                self.disappeared[oid]    = 0
                self.age[oid]           = self.age.get(oid,0) + 1
                if oid not in self.x_history: self.x_history[oid] = []
                self.x_history[oid].append(nx)
                self.y_history[oid].append(ny)
                if gray_frame is not None:
                    h, w = gray_frame.shape
                    x1, y1 = max(0, int(nx - nr)), max(0, int(ny - nr))
                    x2, y2 = min(w, int(nx + nr)), min(h, int(ny + nr))
                    roi_slice = gray_frame[y1:y2, x1:x2]
                    if roi_slice.size > 0:
                        if oid not in self.intensities: self.intensities[oid] = []
                        self.intensities[oid].append(float(np.mean(roi_slice)))
                used_d.add(bi); used_o.add(oid)

                # ── Покадровый лог для ВСЕХ пузырьков (п.2 ТЗ) ──────────────
                if self.enable_per_frame_log:
                    # Расчёт мгновенной скорости через скользящее окно y_history
                    y_hist_pf = list(self.y_history.get(oid, []))
                    win_pf    = len(y_hist_pf)
                    if win_pf > 1:
                        dy_pf  = y_hist_pf[-1] - y_hist_pf[0]    # px (вниз = +)
                        dt_pf  = (win_pf - 1) / max(self.fps, 1)  # секунды
                        vy_pf  = abs(dy_pf) / dt_pf               # px/s (вверх = -)
                    else:
                        vy_pf  = abs(self.velocities.get(oid, (0, 0))[1]) * self.fps

                    v_mm_pf = vy_pf / self.px_per_mm if self.px_per_mm > 0 else 0.0
                    v_cm_pf = v_mm_pf / 10.0

                    self.per_frame_log.append({
                        "frame":       self.frame_n,
                        "time_ms":     round((self.frame_n / max(self.fps, 1)) * 1000.0, 1),
                        "time_s":      round(self.frame_n / max(self.fps, 1), 4),
                        "id":          oid,
                        "x_px":        round(nx, 2),
                        "y_px":        round(ny, 2),
                        "r_px":        round(nr, 2),
                        "a_min_px":    round(n_amin, 3),
                        "a_max_px":    round(n_amax, 3),
                        "angle":       round(n_ang, 1),
                        "shape":       _classify_shape(n_amin, n_amax),
                        "v_px_s":      round(vy_pf, 4),
                        "v_mm_s":      round(v_mm_pf, 4),
                        "v_cm_s":      round(v_cm_pf, 6),
                        "window_frames": win_pf,
                        "age":         self.age.get(oid, 0),
                    })



        # 5. Unmatched tracks → disappeared
        for oid in list(self.positions):
            if oid not in used_o:
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self._dereg(oid)

        # 6. Unmatched dets → новые треки
        for i, d in enumerate(dets):
            if i not in used_d: self._reg(*d)

        # 7. COUNTING LINE CROSSING  ← главная логика
        #    Трек пересёк линию снизу вверх = ещё не counted + трек взрослый
        for oid, (x, y) in list(self.positions.items()):
            if oid in self.counted: continue
            if self.age.get(oid,0) < self.min_track_frames: continue

            prev_y = self.prev_y.get(oid, y)
            # prev_y > line_y: был ниже линии
            # y <= line_y: теперь на уровне или выше
            if prev_y > counting_line_y >= y:
                self.total += 1
                self.counted.add(oid)
                lr = list(self.radii.get(oid,[10]))[-1]
                e_info = list(self.ellipses.get(oid, [(lr, lr, 0.0)]))[-1]
                a_min, a_max, angle = e_info
                ecc = round(a_min / a_max, 2) if a_max > 0 else 1.0

                vx = self.velocities.get(oid, (0.0, 0.0))[0]
                # Окно усреднения скорости (по PDF)
                y_hist = self.y_history.get(oid, [])
                window = len(y_hist)
                if window > 1:
                    dy = y_hist[-1] - y_hist[0]
                    vy = dy / (window - 1)
                else:
                    vy = self.velocities.get(oid, (0.0, 0.0))[1]

                x_hist = self.x_history.get(oid, [])
                wobble = round(float(np.std(x_hist)), 2) if len(x_hist) > 1 else 0.0
                ints = self.intensities.get(oid, [])
                mineralization = round(float(np.mean(ints)), 2) if len(ints) > 0 else 0.0

                # ── Флотационная физика (кадры/пиксели) ─────────────
                # Константы (жидкость = вода при 20°C)
                RHO_LIQUID = 998.0    # кг/м³
                RHO_GAS    = 1.225    # кг/м³ (воздух)
                MU         = 0.001002 # Па·с (динамическая вязкость воды)
                G          = 9.81     # м/с²
                PX2M       = self.px2m   # берётся из калибровки px_per_mm

                # Реальная скорость в м/с
                FPS = self.fps
                r_m  = lr * PX2M                     # радиус в метрах
                v_real_ms = abs(vy) * PX2M * FPS     # скорость в м/с

                # Стоксова терминальная скорость (сферический пузырёк, Re << 1)
                # v_stokes = (2/9) × r² × (ρ_l - ρ_g) × g / μ
                v_stokes_ms = (2.0/9.0) * (r_m**2) * (RHO_LIQUID - RHO_GAS) * G / MU
                v_stokes_px = v_stokes_ms / (PX2M * FPS)  # обратно в px/кадр

                # Δv = v_stokes - v_real (> 0: медленнее теории → нагружен рудой!)
                delta_v = round(v_stokes_ms - v_real_ms, 4)

                # Reynolds number Re = ρ × v × d / μ
                diameter_m = 2 * r_m
                Re = round((RHO_LIQUID * v_real_ms * diameter_m) / MU, 2)

                # Кинетическая энергия пузырька в момент пересечения
                # Ek = 0.5 × m × v², m = (4/3)π r³ ρ_gas
                mass_bubble = (4.0/3.0) * math.pi * (r_m**3) * RHO_GAS
                Ek = round(0.5 * mass_bubble * (v_real_ms**2), 10)  # в Джоулях

                # P_collision ≈ 1.5 × (d_bubble/d_particle)² × (v_bubble / (v_bubble + v_particle))
                # Упрощённо (нет данных о частице — берём фиксированный d_particle = 0.1 мм = 1e-4 м)
                D_PARTICLE = 1e-4
                p_col = min(1.0, 1.5 * (diameter_m / D_PARTICLE)**2 * 0.3)  # 0.3 — коэф. прилипания фр. MIBC

                # Скорость в трёх единицах (п.3 ТЗ)
                v_px_s   = abs(vy) * FPS                         # px/с
                v_mm_s   = v_real_ms * 1000.0                    # мм/с
                v_cm_s   = v_real_ms * 100.0                     # см/с

                # Окно усреднения фактическое
                y_hist_final = self.y_history.get(oid, [])
                win_actual   = len(y_hist_final)

                self.detailed_log.append({
                    # Идентификация
                    "id":              oid,
                    "frame":           self.frame_n,
                    # Время (п.4 ТЗ) — в мс и секундах
                    "time_ms":         round((self.frame_n / FPS) * 1000.0, 1),
                    "time_s":          round(self.frame_n / FPS, 4),
                    # Координаты
                    "x_px":            round(x, 2),
                    "y_px":            round(y, 2),
                    # Размер
                    "r_px":            round(lr, 2),
                    # Форма через fitEllipse (п.5, п.6 ТЗ)
                    "a_min_px":        round(a_min, 3),
                    "a_max_px":        round(a_max, 3),
                    "a_min_mm":        round(a_min * self.px2m * 1000.0, 4),
                    "a_max_mm":        round(a_max * self.px2m * 1000.0, 4),
                    "eccentricity":    ecc,
                    "angle":           round(angle, 1),
                    "shape":           _classify_shape(a_min, a_max),
                    # Скорость (п.3 ТЗ)
                    "vy_px":           round(abs(vy), 4),
                    "v_px_s":          round(v_px_s, 4),
                    "v_mm_s":          round(v_mm_s, 4),
                    "v_cm_s":          round(v_cm_s, 6),
                    "vx_px":           round(vx, 2),
                    # Усреднение (п.7 ТЗ)
                    "window_frames":   win_actual,
                    # Трек
                    "age":             self.age.get(oid, 0),
                    "wobble":          wobble,
                    "mineralization":  mineralization,
                    # Гидродинамика
                    "reynolds":        Re,
                    "v_stokes_px":     round(v_stokes_px, 4),
                    "v_stokes_mms":    round(v_stokes_ms * 1000.0, 4),
                    "delta_v":         delta_v,
                    "kinetic_energy":  Ek,
                })


                # блокируем зону — радиус блокировки = 2 × радиус пузырька
                self._exit_zones.append(
                    (x, y, lr*2, self.frame_n + self.exit_memory))

        return self.positions

    # ── вспомогательные свойства ──────────────────────────────────────
    def avg_radius(self) -> float:
        if not self.counted: return 0.0
        rr = [r for oid in self.counted
              for r in list(self.radii.get(oid, []))]
        return round(sum(rr)/len(rr), 1) if rr else 0.0

    def rate(self, elapsed: float) -> float:
        return round(self.total/elapsed, 2) if elapsed>0 else 0.0

    def current_speed(self, oid: int) -> float:
        """Вертикальная скорость (px/кадр), положительная = вверх."""
        _, vy = self.velocities.get(oid, (0,0))
        return round(-vy, 1)   # -vy: минус потому что вверх = y уменьшается

    @property
    def active_count(self) -> int:
        return len(self.positions)


OUT   = Path("bubble_results")
OUT.mkdir(exist_ok=True)
DETAILS_OUT = OUT / "details"
DETAILS_OUT.mkdir(exist_ok=True)
JFILE = OUT / "sessions.json"
XFILE = OUT / "sessions.xlsx"

def _f2r(v, places: int = 4) -> str:
    """Форматирует число с заменой точки на запятую (для русского Excel)."""
    return str(round(v, places)).replace('.', ',')


def save_detailed_csv(log: list, px_per_mm: float, start_timestamp: str,
                      source_res: str = "—", source_fps: float = 0.0):
    """
    Итоговый CSV: одна строка на каждый пузырёк (момент пересечения счётной линии).
    Содержит время в мс и с, скорость в px/s + мм/с + см/с, форму, полуоси и т.д.
    """
    if not log: return
    safe_time = start_timestamp.replace(":", "-").replace(" ", "_")
    csv_file  = DETAILS_OUT / f"session_{safe_time}.csv"
    with open(csv_file, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([
            "ID", "Frame", "Time_ms", "Time_s",
            "X_px", "Y_px",
            "Radius_px", "Radius_mm",
            # Форма через fitEllipse (п.5, п.6 ТЗ)
            "a_min_px", "a_max_px", "a_min_mm", "a_max_mm",
            "Eccentricity", "Angle_deg", "Shape",
            # Скорость (п.3 ТЗ)
            "Speed_px_s", "Speed_mm_s", "Speed_cm_s", "Speed_X_px_s",
            # Усреднение (п.7 ТЗ)
            "Window_frames",
            # Трек
            "Age_frames", "Wobble_px", "Mineralization_Index",
            # Гидродинамика
            "Reynolds_Number", "V_Stokes_px", "V_Stokes_mm_s",
            "Delta_V_m_s", "Kinetic_Energy_J",
            # Мета (п.1 ТЗ)
            "Source_Resolution", "Source_FPS",
        ])
        for row in log:
            rmm = round(row["r_px"] / px_per_mm, 4) if px_per_mm > 0 else 0.0
            writer.writerow([
                row["id"],
                row["frame"],
                _f2r(row.get("time_ms", 0), 1),
                _f2r(row.get("time_s", 0), 4),
                _f2r(row.get("x_px", 0), 2),
                _f2r(row.get("y_px", 0), 2),
                _f2r(row["r_px"], 3),
                _f2r(rmm, 4),
                _f2r(row.get("a_min_px", row["r_px"]), 3),
                _f2r(row.get("a_max_px", row["r_px"]), 3),
                _f2r(row.get("a_min_mm", 0), 4),
                _f2r(row.get("a_max_mm", 0), 4),
                _f2r(row.get("eccentricity", 1.0), 4),
                _f2r(row.get("angle", 0.0), 1),
                row.get("shape", "circle"),
                _f2r(row.get("v_px_s", 0), 4),
                _f2r(row.get("v_mm_s", 0), 4),
                _f2r(row.get("v_cm_s", 0), 6),
                _f2r(row.get("vx_px", 0), 2),
                row.get("window_frames", 1),
                row.get("age", 0),
                _f2r(row.get("wobble", 0), 3),
                _f2r(row.get("mineralization", 0), 3),
                _f2r(row.get("reynolds", 0), 2),
                _f2r(row.get("v_stokes_px", 0), 4),
                _f2r(row.get("v_stokes_mms", 0), 4),
                _f2r(row.get("delta_v", 0), 6),
                _f2r(row.get("kinetic_energy", 0), 10),
                source_res,
                _f2r(source_fps, 2),
            ])
    print(f"[SAVE CSV] {csv_file}")


def save_per_frame_csv(log: list, px_per_mm: float, start_timestamp: str,
                       source_res: str = "—", source_fps: float = 0.0):
    """
    Покадровый CSV: строка на каждый кадр × каждый пузырёк.
    Позволяет сопоставить кадры видео с характеристиками пузырьков (п.4 ТЗ).
    Содержит ВСЕ активные пузырьки, включая не достигшие счётной линии.
    """
    if not log: return
    safe_time = start_timestamp.replace(":", "-").replace(" ", "_")
    csv_file  = DETAILS_OUT / f"per_frame_{safe_time}.csv"
    with open(csv_file, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([
            "Frame", "Time_ms", "Time_s",
            "BubbleID",
            "X_px", "Y_px", "Radius_px",
            "a_min_px", "a_max_px", "Angle_deg", "Shape",
            "Speed_px_s", "Speed_mm_s", "Speed_cm_s",
            "Window_frames", "Age_frames",
            "Source_Resolution", "Source_FPS",
        ])
        for row in log:
            writer.writerow([
                row["frame"],
                _f2r(row.get("time_ms", 0), 1),
                _f2r(row.get("time_s", 0), 4),
                row["id"],
                _f2r(row.get("x_px", 0), 2),
                _f2r(row.get("y_px", 0), 2),
                _f2r(row.get("r_px", 0), 3),
                _f2r(row.get("a_min_px", 0), 3),
                _f2r(row.get("a_max_px", 0), 3),
                _f2r(row.get("angle", 0), 1),
                row.get("shape", "circle"),
                _f2r(row.get("v_px_s", 0), 4),
                _f2r(row.get("v_mm_s", 0), 4),
                _f2r(row.get("v_cm_s", 0), 6),
                row.get("window_frames", 1),
                row.get("age", 0),
                source_res,
                _f2r(source_fps, 2),
            ])
    print(f"[SAVE PER-FRAME CSV] {csv_file}  ({len(log)} строк)")



def save_session(s: dict):
    data = []
    if JFILE.exists():
        try: data = json.loads(JFILE.read_text("utf-8"))
        except: pass
    data.append(s)
    JFILE.write_text(json.dumps(data,indent=4,ensure_ascii=False),"utf-8")

    wb = openpyxl.load_workbook(XFILE) if XFILE.exists() else openpyxl.Workbook()
    if "Sheet" in wb.sheetnames and len(wb.sheetnames)==1: del wb["Sheet"]
    sn = "Сессии"
    if sn not in wb.sheetnames:
        ws = wb.create_sheet(sn)
        hdrs=["Дата","Источник","Разрешение","FPS","px/мм","Длит.(с)","Пузырьков",
              "Ср.радиус(px)","Пузырьк/с","ROI","Заметки"]
        ws.append(hdrs)
        hf=PatternFill("solid",fgColor="1A237E")
        hfnt=Font(bold=True,color="FFFFFF",size=11)
        thin=Side(style="thin",color="AAAAAA")
        brd=Border(left=thin,right=thin,top=thin,bottom=thin)
        for ci,cell in enumerate(ws[1],1):
            cell.fill=hf; cell.font=hfnt
            cell.alignment=Alignment(horizontal="center",vertical="center")
            cell.border=brd
            ws.column_dimensions[cell.column_letter].width=max(16,len(hdrs[ci-1])+4)
        ws.row_dimensions[1].height=22
    else:
        ws=wb[sn]
    thin=Side(style="thin",color="AAAAAA")
    brd=Border(left=thin,right=thin,top=thin,bottom=thin)
    row=[s.get("start_time"),s.get("source"),
         f"{s.get('res_w',0)}x{s.get('res_h',0)}", s.get("fps_actual",0.0), s.get("px_per_mm",0.0),
         s.get("duration_sec"),s.get("total_bubbles"),s.get("avg_radius_px"),s.get("rate_per_sec"),
         s.get("roi"),s.get("notes")]
    ws.append(row)
    lr=ws.max_row
    ef=PatternFill("solid",fgColor="E8EAF6")
    for cell in ws[lr]:
        if lr%2==0: cell.fill=ef
        cell.border=brd
        cell.alignment=Alignment(horizontal="center",vertical="center")
    wb.save(XFILE)
    print(f"[SAVE] {JFILE}  |  {XFILE}")




# ═══════════════════════════════════════════════════════════════════════════
#  ДИАЛОГ РУЧНОЙ КАЛИБРОВКИ (Вариант А по ТЗ)
# ═══════════════════════════════════════════════════════════════════════════
class CalibrationDialog:
    """
    Ручная калибровка масштаба px/мм.

    Алгоритм:
      1. Показывает первый кадр видео (или последний сохранённый).
      2. Пользователь проводит линию мышью по эталонному объекту (линейка, объект
         известного размера).
      3. Вводит реальную длину линии в мм.
      4. Программа считает: px_per_mm = pixel_length / real_mm.
    """
    BG = "#000000"; FG = "#E0E0FF"; ACC = "#7C4DFF"; ENT = "#0a0a0a"
    FONT = ("Segoe UI", 10); H1 = ("Segoe UI", 11, "bold")

    def __init__(self, master, video_path: str):
        self.result_px_per_mm: float = 0.0  # 0 = отмена
        self._pt1 = None
        self._pt2 = None
        self._img_orig  = None   # BGR numpy array
        self._img_disp  = None   # отображаемая (уменьшенная) копия
        self._scale_xy  = (1.0, 1.0)  # коэфф масштабирования для обратного пересчёта

        # ── попытка загрузить первый кадр видео ────────────────────────
        self._frame = self._load_frame(video_path)
        if self._frame is None:
            messagebox.showwarning("Калибровка",
                "Не удалось загрузить кадр видео.\n"
                "Укажите px/мм вручную в поле ниже.")
            return

        # ── окно Toplevel ───────────────────────────────────────────────
        self.root = tk.Toplevel(master)
        self.root.title("📐  Калибровка масштаба — проведите линию по эталону")
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{min(1000,sw-80)}x{min(720,sh-80)}+40+40")

        self._build()
        master.wait_window(self.root)

    @staticmethod
    def _load_frame(video_path: str):
        """Читает первый кадр из видеофайла."""
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return None
            # Перемещаемся немного вперёд, чтобы избежать чёрного кадра
            cap.set(cv2.CAP_PROP_POS_FRAMES, 5)
            ok, frame = cap.read()
            cap.release()
            return frame if ok else None
        except Exception:
            return None

    def _build(self):
        B, F, A, E = self.BG, self.FG, self.ACC, self.ENT

        # ── Инструкция ──────────────────────────────────────────────────
        top = tk.Frame(self.root, bg=B)
        top.pack(fill="x", padx=12, pady=8)
        tk.Label(top,
                 text="📐  Нарисуйте линию по объекту известного размера  →  введите длину в мм",
                 font=self.H1, bg=B, fg=A).pack(side="left")

        # ── Canvas для отображения кадра ────────────────────────────────
        canv_frame = tk.Frame(self.root, bg=B)
        canv_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._canvas = tk.Canvas(canv_frame, bg="#000010",
                                  cursor="crosshair", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>",   self._on_resize)
        self._canvas.bind("<ButtonPress-1>",  self._on_click)
        self._canvas.bind("<B1-Motion>",      self._on_drag)
        self._canvas.bind("<ButtonRelease-1>",self._on_release)

        # ── Панель ввода ─────────────────────────────────────────────────
        bot = tk.Frame(self.root, bg=B)
        bot.pack(fill="x", padx=12, pady=8)

        tk.Label(bot, text="Длина нарисованной линии (мм):",
                 font=self.FONT, bg=B, fg=F).pack(side="left", padx=(0, 8))
        self._len_var = tk.StringVar(value="10")
        tk.Entry(bot, textvariable=self._len_var, font=self.FONT,
                 bg=E, fg=F, insertbackground=F,
                 relief="flat", bd=4, width=8).pack(side="left")

        # Показать вычисленный px/мм
        self._result_lbl = tk.Label(bot, text="→  нарисуйте линию",
                                     font=self.FONT, bg=B, fg="#6868A0")
        self._result_lbl.pack(side="left", padx=14)

        # Кнопки
        tk.Button(bot, text="✅  Применить", font=self.FONT,
                  bg=A, fg="white", bd=0, padx=10, pady=6, cursor="hand2",
                  command=self._apply).pack(side="right", padx=(8, 0))
        tk.Button(bot, text="↺  Очистить", font=self.FONT,
                  bg="#333355", fg=F, bd=0, padx=10, pady=6, cursor="hand2",
                  command=self._clear).pack(side="right", padx=4)

        self._line_id = None
        self._render_frame()

    def _render_frame(self):
        """Масштабирует BGR-кадр под текущий размер canvas и рисует его."""
        self.root.update_idletasks()
        cw = self._canvas.winfo_width()  or 800
        ch = self._canvas.winfo_height() or 500
        fh, fw = self._frame.shape[:2]
        sc = min(cw / fw, ch / fh, 1.0)
        nw, nh = max(1, int(fw * sc)), max(1, int(fh * sc))
        self._scale_xy = (fw / nw, fh / nh)
        disp = cv2.resize(self._frame, (nw, nh))
        disp_rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
        from PIL import Image, ImageTk  # ленивый импорт
        img = ImageTk.PhotoImage(Image.fromarray(disp_rgb))
        self._canvas.delete("all")
        ox = (cw - nw) // 2
        oy = (ch - nh) // 2
        self._ox, self._oy = ox, oy
        self._canvas.create_image(ox, oy, anchor="nw", image=img)
        self._canvas._img_ref = img  # предотвращаем GC
        if self._pt1 and self._pt2:
            self._draw_line()

    def _on_resize(self, _):
        self._render_frame()

    def _on_click(self, e):
        self._pt1 = (e.x, e.y)
        self._pt2 = None
        if self._line_id:
            self._canvas.delete(self._line_id)

    def _on_drag(self, e):
        self._pt2 = (e.x, e.y)
        self._draw_line()
        self._update_preview()

    def _on_release(self, e):
        self._pt2 = (e.x, e.y)
        self._draw_line()
        self._update_preview()

    def _draw_line(self):
        if self._line_id:
            self._canvas.delete(self._line_id)
        # Удаляем все старые вспомогательные элементы
        self._canvas.delete("calib_aux")
        if self._pt1 and self._pt2:
            x1, y1 = self._pt1
            x2, y2 = self._pt2
            # Сплошная прямая линия (белая с жёлтой обводкой — хорошо видна на любом фоне)
            self._canvas.create_line(
                x1, y1, x2, y2,
                fill="#333333", width=5, tags="calib_aux")  # тень
            self._line_id = self._canvas.create_line(
                x1, y1, x2, y2,
                fill="#FFFFFF", width=2, tags="calib_aux")
            # Концевые точки — крестики с кругом
            for pt in (self._pt1, self._pt2):
                px, py = pt
                self._canvas.create_oval(
                    px-6, py-6, px+6, py+6,
                    fill="", outline="#FFD700", width=2, tags="calib_aux")
                self._canvas.create_line(px-8, py, px+8, py,
                    fill="#FFD700", width=2, tags="calib_aux")
                self._canvas.create_line(px, py-8, px, py+8,
                    fill="#FFD700", width=2, tags="calib_aux")
            # Подпись длины в середине линии
            mx, my = (x1+x2)//2, (y1+y2)//2
            px_len = self._px_length()
            try:
                real_mm = float(self._len_var.get().replace(',', '.'))
            except ValueError:
                real_mm = 0.0
            label = f" {px_len:.0f} px"
            if real_mm > 0:
                label += f" / {real_mm:.1f} мм = {px_len/real_mm:.2f} px/мм"
            self._canvas.create_text(
                mx+1, my+14, text=label,
                fill="#111111", font=("Segoe UI", 9, "bold"),
                anchor="n", tags="calib_aux")
            self._canvas.create_text(
                mx, my+13, text=label,
                fill="#FFD700", font=("Segoe UI", 9, "bold"),
                anchor="n", tags="calib_aux")

    def _px_length(self) -> float:
        """Длина линии в пикселях исходного кадра."""
        if not (self._pt1 and self._pt2):
            return 0.0
        dx = (self._pt2[0] - self._pt1[0]) * self._scale_xy[0]
        dy = (self._pt2[1] - self._pt1[1]) * self._scale_xy[1]
        return math.hypot(dx, dy)

    def _update_preview(self):
        px = self._px_length()
        try:
            real_mm = float(self._len_var.get().replace(',', '.'))
        except ValueError:
            real_mm = 0.0
        if px > 0 and real_mm > 0:
            ppmm = px / real_mm
            self._result_lbl.config(
                text=f"→  {px:.1f} px  /  {real_mm} мм  =  {ppmm:.3f} px/мм",
                fg="#00E676")
        else:
            self._result_lbl.config(text="→  нарисуйте линию и введите длину",
                                     fg="#6868A0")

    def _clear(self):
        self._pt1 = self._pt2 = None
        if self._line_id:
            self._canvas.delete(self._line_id)
            self._line_id = None
        self._result_lbl.config(text="→  нарисуйте линию", fg="#6868A0")

    def _apply(self):
        px = self._px_length()
        try:
            real_mm = float(self._len_var.get().replace(',', '.'))
        except ValueError:
            real_mm = 0.0
        if px <= 0 or real_mm <= 0:
            messagebox.showwarning("Калибровка",
                "Нарисуйте линию и введите длину в мм!")
            return
        self.result_px_per_mm = round(px / real_mm, 4)
        messagebox.showinfo("Калибровка",
            f"Масштаб установлен: {self.result_px_per_mm:.4f} px/мм\n"
            f"({px:.1f} px = {real_mm} мм)")
        self.root.destroy()


class SettingsDialog:

    BG="#000000"; FG="#E0E0FF"; ACC="#7C4DFF"; ENT="#0a0a0a"
    FONT=("Segoe UI",10); H1=("Segoe UI",12,"bold")

    def __init__(self, master=None):
        self.result = None
        # Если передан master — используем Toplevel (один root на всё приложение)
        if master is not None:
            self.root = tk.Toplevel(master)
            self._is_toplevel = True
        else:
            self.root = tk.Tk()
            self._is_toplevel = False
        self.root.title("Bubble Tracker PRO v5.0 — Counting Line")
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        self.root.minsize(580, 420)
        sw=self.root.winfo_screenwidth(); sh=self.root.winfo_screenheight()
        w,h=690,min(780,sh-80)
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self._build()
        if self._is_toplevel:
            master.wait_window(self.root)   # блокируемся до закрытия Toplevel
        else:
            self.root.mainloop()

    def _build(self):
        B,F,A,E=self.BG,self.FG,self.ACC,self.ENT
        FONT,H1=self.FONT,self.H1

        # HEADER
        hdr=tk.Frame(self.root,bg=B); hdr.pack(side="top",fill="x")
        tk.Label(hdr,text="🫧  BUBBLE TRACKER PRO v5.0",
                 font=("Segoe UI",16,"bold"),bg=B,fg=A).pack(pady=(12,2))
        tk.Label(hdr,text="Counting Line Edition — двойной счёт невозможен",
                 font=FONT,bg=B,fg="#9090C0").pack(pady=(0,8))
        ttk.Separator(hdr).pack(fill="x",padx=16,pady=(0,4))

        # FOOTER
        ftr=tk.Frame(self.root,bg=B); ftr.pack(side="bottom",fill="x")
        ttk.Separator(ftr).pack(fill="x",padx=16,pady=(4,0))
        btn=tk.Frame(ftr,bg=B); btn.pack(pady=10)
        tk.Button(btn,text="   ▶   ЗАПУСТИТЬ   ",
                  font=("Segoe UI",12,"bold"),bg=A,fg="white",
                  bd=0,padx=12,pady=9,cursor="hand2",
                  command=self._submit).pack(side="left",padx=(30,10))
        tk.Button(btn,text="   ✕   ОТМЕНА   ",
                  font=("Segoe UI",11),bg="#333355",fg=F,
                  bd=0,padx=10,pady=9,cursor="hand2",
                  command=self.root.destroy).pack(side="left",padx=10)

        # SCROLL AREA
        wrap=tk.Frame(self.root,bg=B); wrap.pack(side="top",fill="both",expand=True)
        cv=tk.Canvas(wrap,bg=B,highlightthickness=0)
        sb=ttk.Scrollbar(wrap,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); cv.pack(side="left",fill="both",expand=True)
        inner=tk.Frame(cv,bg=B)
        wid=cv.create_window((0,0),window=inner,anchor="nw")
        inner.bind("<Configure>",lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",   lambda e: cv.itemconfig(wid,width=e.width))
        cv.bind_all("<MouseWheel>",
                    lambda e: cv.yview_scroll(int(-1*(e.delta/120)),"units"))

        def sec(txt,pt=10):
            tk.Label(inner,text=txt,font=H1,bg=B,fg=A
                     ).pack(anchor="w",padx=22,pady=(pt,2))
        def hint(txt):
            tk.Label(inner,text="  "+txt,font=("Segoe UI",9),
                     bg=B,fg="#6868A0",wraplength=630,justify="left"
                     ).pack(anchor="w",padx=22)
        def mkrow(label,wfn):
            f=tk.Frame(inner,bg=B); f.pack(fill="x",padx=22,pady=3)
            tk.Label(f,text=label,font=FONT,bg=B,fg=F,
                     width=38,anchor="w").pack(side="left")
            w=wfn(f); w.pack(side="left",fill="x",expand=True); return w
        def mkentry(p,default,width=14):
            e=tk.Entry(p,font=FONT,bg=E,fg=F,
                       insertbackground=F,relief="flat",bd=4,width=width)
            e.insert(0,str(default)); return e
        def mkcheck(p,var):
            return tk.Checkbutton(p,variable=var,font=FONT,bg=B,fg=F,
                                  selectcolor=E,activebackground=B,activeforeground=F)

        # ── SOURCE ─────────────────────────────────────────────────
        sec("📹  Источник видео", 6)
        self._src=tk.StringVar(value="video")
        rb=tk.Frame(inner,bg=B); rb.pack(anchor="w",padx=22,pady=3)
        for txt,val in [("Видео файл","video"),("Веб-камера","camera")]:
            tk.Radiobutton(rb,text=txt,variable=self._src,value=val,
                           font=FONT,bg=B,fg=F,selectcolor=E,
                           activebackground=B,activeforeground=F,
                           command=self._toggle).pack(side="left",padx=10)

        self._src_box=tk.Frame(inner,bg=B); self._src_box.pack(fill="x",padx=22,pady=2)
        self._vf=tk.Frame(self._src_box,bg=B)
        tk.Label(self._vf,text="Путь к файлу:",font=FONT,bg=B,
                 fg=F,width=38,anchor="w").pack(side="left")
        self._fvar=tk.StringVar(value="my_video.mp4")
        tk.Entry(self._vf,textvariable=self._fvar,font=FONT,bg=E,
                 fg=F,insertbackground=F,relief="flat",bd=4
                 ).pack(side="left",fill="x",expand=True)
        tk.Button(self._vf,text="📂",font=FONT,bg=A,fg="white",
                  bd=0,cursor="hand2",command=self._browse
                  ).pack(side="left",padx=(6,0))

        self._cf=tk.Frame(self._src_box,bg=B)
        tk.Label(self._cf,text="Индекс камеры (0=первая):",font=FONT,
                 bg=B,fg=F,width=38,anchor="w").pack(side="left")
        self._cam=mkentry(self._cf,0,8); self._cam.pack(side="left")

        # ── Настройки камеры (разрешение + FPS) ───────────────────
        self._cam_settings = tk.Frame(inner, bg=B)
        self._cam_settings.pack(fill="x", padx=22, pady=(2, 0))

        tk.Label(self._cam_settings, text="Разрешение камеры:",
                 font=FONT, bg=B, fg=F, width=38, anchor="w").pack(side="left")
        self._cam_res = tk.StringVar(value="Auto")
        res_opts = ["Auto","3840x2160","1920x1080","1280x720","1024x768","640x480","320x240"]
        ttk.Combobox(self._cam_settings, textvariable=self._cam_res,
                     values=res_opts, state="readonly", width=16,
                     font=FONT).pack(side="left")

        self._cam_fps_frame = tk.Frame(inner, bg=B)
        self._cam_fps_frame.pack(fill="x", padx=22, pady=2)
        tk.Label(self._cam_fps_frame, text="FPS камеры:",
                 font=FONT, bg=B, fg=F, width=38, anchor="w").pack(side="left")
        self._cam_fps = tk.StringVar(value="Auto")
        fps_opts = ["Auto","240","120","60","30","25","15"]
        ttk.Combobox(self._cam_fps_frame, textvariable=self._cam_fps,
                     values=fps_opts, state="readonly", width=10,
                     font=FONT).pack(side="left")
        tk.Label(self._cam_fps_frame,
                 text="  (для видеофайла FPS читается автоматически)",
                 font=("Segoe UI",9), bg=B, fg="#6868A0").pack(side="left")

        self._toggle()


        # ── ROI ────────────────────────────────────────────────────
        sec("🎯  Зона детекции (ROI)")
        hint("Все нули = весь кадр. ЛКМ тянуть = нарисовать зону прямо на видео. ПКМ = сброс.")
        roi_f=tk.Frame(inner,bg=B); roi_f.pack(fill="x",padx=22,pady=4)
        self._roi=[]
        for lbl,df in zip(["X:","Y:","Ширина:","Высота:"],[0,0,0,0]):
            tk.Label(roi_f,text=lbl,font=FONT,bg=B,fg=F).pack(side="left",padx=(8,2))
            e=tk.Entry(roi_f,font=FONT,bg=E,fg=F,
                       insertbackground=F,relief="flat",bd=4,width=7)
            e.insert(0,str(df)); e.pack(side="left"); self._roi.append(e)

        # ── COUNTING LINE ──────────────────────────────────────────
        sec("📐  Счётная линия")
        hint("Пузырёк считается ровно 1 раз при пересечении этой линии снизу вверх. "
             "Ползунок \'LineY%\' в окне видео позволяет двигать линию в реальном времени.")
        self._line_pct=mkrow("Позиция линии (% от верха кадра):",
                             lambda p: mkentry(p,25))

        # ── ДЕТЕКТОР ──────────────────────────────────────────────
        sec("⚙️  Параметры детектора")
        hint("Оптимально для тёмных пузырьков на светлом фоне: min=3, max=20, param2=15.")
        self._minr  =mkrow("Мин. радиус пузырька (px):", lambda p:mkentry(p,3))
        self._maxr  =mkrow("Макс. радиус пузырька (px):", lambda p:mkentry(p,25))
        self._p2    =mkrow("HoughCircles param2 (5–50):",  lambda p:mkentry(p,15))
        self._mincirc=mkrow("Мин. круглость контуров (0–1):", lambda p:mkentry(p,0.40))
        self._mintracks=mkrow("Мин. кадров до подсчёта:",  lambda p:mkentry(p,3))

        # ── 6-STAGE PIPELINE ──────────────────────────────────────
        sec("🔬  Pipeline")
        hint("CLAHE и Top-Hat всегда включены. MOG2 убирает статичный фон (рекомендуется).")
        self._use_mog2=tk.BooleanVar(value=True)
        self._use_of  =tk.BooleanVar(value=False)
        ff=tk.Frame(inner,bg=B); ff.pack(fill="x",padx=22,pady=4)
        for var,txt,sub in [(self._use_mog2,"MOG2 вычитание фона",
                              " (убирает стенки, подложку — важно!)"),
                             (self._use_of,"Optical Flow верификация",
                              " (дороже CPU, включать при ложных срабатываниях)")]:
            row=tk.Frame(ff,bg=B); row.pack(anchor="w",pady=2)
            mkcheck(row,var).pack(side="left")
            tk.Label(row,text=txt,font=FONT,bg=B,fg=F).pack(side="left")
            tk.Label(row,text=sub,font=("Segoe UI",9),bg=B,fg="#6868A0").pack(side="left")
        self._mog2_hist=mkrow("MOG2 история (кадров):",     lambda p:mkentry(p,200))
        self._mog2_thr =mkrow("MOG2 чувствительность:",     lambda p:mkentry(p,16))

        # ── ПРОИЗВОДИТЕЛЬНОСТЬ ────────────────────────────────────
        sec("🚀  Производительность")
        hint("Масштаб 1.0 = максимальное качество. 0.5 = в 4× быстрее. "
             "Для мелких пузырьков ставьте 0.75–1.0.")
        self._sc =mkrow("Масштаб анализа (0.25–1.0):",       lambda p:mkentry(p,0.8))
        self._sk =mkrow("Пропуск кадров (1=каждый):",        lambda p:mkentry(p,1))
        self._spd=mkrow("Задержка воспр. (мс):",             lambda p:mkentry(p,25))

        # ── КАЛИБРОВКА ────────────────────────────────────────────
        sec("📏  Калибровка масштаба (px ↔ мм)")
        hint("Вариант А (рекомендуется): нажмите кнопку — откроется кадр видео, "
             "нарисуйте линию по линейке/объекту известного размера, введите длину в мм.")

        # Кнопка запуска CalibrationDialog
        cal_btn_frame = tk.Frame(inner, bg=B)
        cal_btn_frame.pack(fill="x", padx=22, pady=(4, 2))
        self._cal_result_lbl = tk.Label(cal_btn_frame,
                                         text="Калибровка не выполнена — будет использовано поле px/мм ниже",
                                         font=("Segoe UI", 9), bg=B, fg="#6868A0")
        self._cal_result_lbl.pack(side="left", padx=(0, 12))
        tk.Button(cal_btn_frame, text="📐  Калибровать на кадре...",
                  font=FONT, bg=A, fg="white", bd=0, padx=10, pady=5,
                  cursor="hand2", command=self._open_calibration
                  ).pack(side="right")

        hint("Вариант Б (ручной): введите px/мм напрямую ниже (0 = не использовать).")
        self._px_per_mm_manual = mkrow("px/мм вручную (0=авто):", lambda p: mkentry(p, 0.0))
        self._ref_mm = mkrow("Диаметр эталона (мм, 0=авто):",  lambda p: mkentry(p, 0))

        # ── ПОКАДРОВЫЙ ЛОГ ────────────────────────────────────────
        sec("📂  Покадровый лог (все пузырьки, каждый кадр)")
        hint("Сохраняет per_frame_*.csv — строку на каждый пузырёк в каждом кадре. "
             "Позволяет сопоставить кадры видео с характеристиками (п.4 ТЗ). "
             "Файл может быть большим (до 10 000+ строк для 15–23 сек. видео).")
        self._save_per_frame = tk.BooleanVar(value=True)
        fpf = tk.Frame(inner, bg=B); fpf.pack(anchor="w", padx=22, pady=2)
        mkcheck(fpf, self._save_per_frame).pack(side="left")
        tk.Label(fpf, text="Сохранять покадровый лог (per_frame_*.csv)",
                 font=FONT, bg=B, fg=F).pack(side="left")



        # ── СЕГМЕНТАЦИЯ СЛИПШИХСЯ ─────────────────────────────────
        sec("🔀  Разделение перекрывающихся пузырьков")
        hint("Convexity Defects — быстрый метод разрезания по вогнутостям. "
             "Watershed — точный метод для кластеров 3+ пузырьков (требует scipy + scikit-image).")
        self._use_conv = tk.BooleanVar(value=True)
        self._use_ws   = tk.BooleanVar(value=True)
        fseg=tk.Frame(inner,bg=B); fseg.pack(fill="x",padx=22,pady=4)
        for var,txt,sub in [
            (self._use_conv, "Convexity Defects"," (быстро, без доп. библиотек)"),
            (self._use_ws,   "Enhanced Watershed"," (scipy + scikit-image, точнее)")]:
            row=tk.Frame(fseg,bg=B); row.pack(anchor="w",pady=2)
            mkcheck(row,var).pack(side="left")
            tk.Label(row,text=txt,font=FONT,bg=B,fg=F).pack(side="left")
            tk.Label(row,text=sub,font=("Segoe UI",9),bg=B,fg="#6868A0").pack(side="left")
        self._conv_depth = mkrow("Глубина дефекта (0.05–0.40):",  lambda p:mkentry(p,0.15))

        # ── ТРЕВОГИ ──────────────────────────────────────────────
        sec("🚨  Умная система тревог")
        hint("При превышении порогов программа выведет красное предупреждение прямо на видео.")
        self._alarm_re = mkrow("Тревога если Re > (0=выкл):",    lambda p:mkentry(p,700))
        self._alarm_big = mkrow("Тревога если крупных % > (0=выкл):", lambda p:mkentry(p,30))
        self._alarm_min = mkrow("Тревога если Минерализация < (0=выкл):", lambda p:mkentry(p,40))

        # ── ЖИВОЙ ГРАФИК ────────────────────────────────────────
        sec("📊  Живой мини-график на видео")
        hint("Рисует осциллоскоп скорости и размера прямо поверх видеоокна.")
        self._live_chart = tk.BooleanVar(value=True)
        ff2=tk.Frame(inner,bg=B); ff2.pack(anchor="w",padx=22,pady=4)
        mkcheck(ff2, self._live_chart).pack(side="left")
        tk.Label(ff2,text="Показывать живой график",font=FONT,bg=B,fg=F).pack(side="left")
        self._chart_width = mkrow("Ширина истории (кадров):",    lambda p:mkentry(p,120))

        # ── ТЕПЛОВАЯ КАРТА ───────────────────────────────────────
        sec("🗺️  Тепловая карта в конце сессии")
        hint("Сохраняет PNG с наложением горячих зон пузырьков на финальный кадр.")
        self._save_heatmap = tk.BooleanVar(value=True)
        fh=tk.Frame(inner,bg=B); fh.pack(anchor="w",padx=22,pady=4)
        mkcheck(fh, self._save_heatmap).pack(side="left")
        tk.Label(fh,text="Сохранять тепловую карту",font=FONT,bg=B,fg=F).pack(side="left")
        self._heatmap_radius = mkrow("Радиус пятна (px):",       lambda p:mkentry(p,20))

        # ── ЗАПИСЬ ВИДЕО ─────────────────────────────────────────
        sec("🎥  Запись аннотированного видео")
        hint("Фоновая запись MP4 с контурами, ID и счётной линией. Тяжелее по CPU.")
        self._record_video = tk.BooleanVar(value=False)
        fr=tk.Frame(inner,bg=B); fr.pack(anchor="w",padx=22,pady=4)
        mkcheck(fr, self._record_video).pack(side="left")
        tk.Label(fr,text="Записывать видео с аннотациями",font=FONT,bg=B,fg=F).pack(side="left")
        self._rec_fps = mkrow("FPS записи:",                     lambda p:mkentry(p,25))

        # ── ЗАМЕТКИ ──────────────────────────────────────────────
        sec("📝  Заметки к сессии")
        nf=tk.Frame(inner,bg=B); nf.pack(fill="x",padx=22,pady=(2,20))
        self._notes=tk.Entry(nf,font=FONT,bg=E,fg=F,
                             insertbackground=F,relief="flat",bd=4)
        self._notes.pack(fill="x")

    def _toggle(self):
        if self._src.get()=="video":
            self._cf.pack_forget(); self._vf.pack(fill="x")
            # Скрываем настройки камеры (FPS/разрешение не нужны для файла)
            self._cam_settings.pack_forget()
            self._cam_fps_frame.pack_forget()
        else:
            self._vf.pack_forget(); self._cf.pack(fill="x")
            # Показываем настройки камеры
            self._cam_settings.pack(fill="x", padx=22, pady=(2,0))
            self._cam_fps_frame.pack(fill="x", padx=22, pady=2)

    def _browse(self):
        p=filedialog.askopenfilename(
            title="Выберите видео файл",
            filetypes=[("Видео","*.mp4 *.avi *.mov *.mkv *.wmv"),("Все","*.*")])
        if p: self._fvar.set(p)

    def _open_calibration(self):
        """Open CalibrationDialog for manual px/mm setup (Variant A)."""
        video_path = self._fvar.get().strip()
        if self._src.get() != "video" or not video_path:
            messagebox.showinfo("Калибровка",
                "Калибровка на кадре работает только с видеофайлом.\n"
                "Сначала выберите видео файл.")
            return
        dlg = CalibrationDialog(self.root, video_path)
        if dlg.result_px_per_mm > 0:
            # Записываем вычисленное значение в поле ручного ввода
            self._px_per_mm_manual.delete(0, "end")
            self._px_per_mm_manual.insert(0, str(dlg.result_px_per_mm))
            self._cal_result_lbl.config(
                text=f"✅ Калибровка: {dlg.result_px_per_mm:.4f} px/мм",
                fg="#00E676")


    def _i(self,w,d=0):
        try: return int(w.get())
        except: return d
    def _f(self,w,d=0.0):
        try: return float(w.get())
        except: return d

    def _submit(self):
        # Определяем px_per_mm: приоритет — ручной ввод, если 0 — расчёт через эталон
        px_mm_manual = max(0.0, self._f(self._px_per_mm_manual, 0.0))
        ref_mm_val   = max(0.0, self._f(self._ref_mm, 0))
        min_r        = max(1, self._i(self._minr, 3))
        if px_mm_manual > 0:
            px_per_mm = px_mm_manual
        elif ref_mm_val > 0:
            px_per_mm = min_r / (ref_mm_val / 2.0)
        else:
            px_per_mm = 10.0   # значение по умолчанию

        # Настройки камеры
        cam_res = self._cam_res.get()
        cam_fps_s = self._cam_fps.get()
        cam_w, cam_h = 0, 0
        cam_fps_val  = 0
        if cam_res != "Auto":
            try:
                cam_w, cam_h = map(int, cam_res.split("x"))
            except Exception:
                cam_w, cam_h = 0, 0
        if cam_fps_s != "Auto":
            try:
                cam_fps_val = int(cam_fps_s)
            except Exception:
                cam_fps_val = 0

        cfg = dict(
            source_type   = self._src.get(),
            video_path    = self._fvar.get().strip(),
            camera_idx    = self._i(self._cam, 0),
            cam_width     = cam_w,
            cam_height    = cam_h,
            cam_fps_set   = cam_fps_val,
            roi           = dict(x=self._i(self._roi[0]), y=self._i(self._roi[1]),
                                  w=self._i(self._roi[2]), h=self._i(self._roi[3])),
            line_pct      = max(1, min(99, self._i(self._line_pct, 25))),
            min_radius    = min_r,
            max_radius    = max(5, self._i(self._maxr, 25)),
            param2        = max(5, self._i(self._p2, 15)),
            min_circularity= max(0.1, min(1.0, self._f(self._mincirc, 0.40))),
            min_track_frames= max(1, self._i(self._mintracks, 3)),
            use_mog2      = self._use_mog2.get(),
            use_of        = self._use_of.get(),
            mog2_history  = max(50, self._i(self._mog2_hist, 200)),
            mog2_thresh   = max(5,  self._i(self._mog2_thr, 16)),
            scale         = max(0.1, min(1.0, self._f(self._sc, 0.8))),
            skip_frames   = max(1, self._i(self._sk, 1)),
            delay_ms      = max(1, self._i(self._spd, 25)),
            px_per_mm     = px_per_mm,
            use_convexity_split = self._use_conv.get(),
            use_watershed       = self._use_ws.get(),
            convex_depth_ratio  = max(0.05, min(0.40, self._f(self._conv_depth, 0.15))),
            alarm_re      = max(0, self._i(self._alarm_re, 700)),
            alarm_big_pct = max(0, self._i(self._alarm_big, 30)),
            alarm_min_idx = max(0, self._i(self._alarm_min, 40)),
            live_chart    = self._live_chart.get(),
            chart_width   = max(30, self._i(self._chart_width, 120)),
            save_heatmap  = self._save_heatmap.get(),
            heatmap_radius= max(5, self._i(self._heatmap_radius, 20)),
            record_video  = self._record_video.get(),
            rec_fps       = max(1, self._i(self._rec_fps, 25)),
            save_per_frame= self._save_per_frame.get(),
            notes         = self._notes.get().strip(),
        )
        if cfg["source_type"] == "video" and not cfg["video_path"]:
            messagebox.showerror("Ошибка", "Укажите путь к видео файлу!")
            return
        self.result = cfg; self.root.destroy()

# ═══════════════════════════════════════════════════════════════════════════
#  ГЛАВНОЕ ПРИЛОЖЕНИЕ
# ═══════════════════════════════════════════════════════════════════════════
import queue as _queue_mod

class BubbleTrackerApp:
    WIN = "Bubble Tracker PRO v5.0"

    def __init__(self, cfg, app_root=None):
        self.cfg = cfg
        self._app_root = app_root   # единый Tk root всего приложения

        self.t0      = time.time()
        self.frame_n = 0
        self._paused    = False
        self._drawing   = False
        self._roi_start = None
        self._tmp_roi   = None
        self._last_frame = None   # для freeze на паузе
        self._det_counts = {"hough":0,"contour":0,"blob":0}
        self._seek_target = None  # атомарная переменная: GUI пишет, worker читает

        # ── источник ──────────────────────────────────────────────
        if cfg["source_type"]=="video":
            # VideoCapture через FFmpeg — все операции cap ТОЛЬКО в worker thread
            self.cap = cv2.VideoCapture(cfg["video_path"], cv2.CAP_FFMPEG)
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(cfg["video_path"])  # fallback
            if not self.cap.isOpened():
                raise FileNotFoundError(f"Файл не найден: {cfg['video_path']}")
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.total      = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.fps_actual = float(self.cap.get(cv2.CAP_PROP_FPS))
            if self.fps_actual <= 0: self.fps_actual = 25.0
            self.res_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.res_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.is_file = True
        else:
            self.cap=cv2.VideoCapture(cfg["camera_idx"])
            if not self.cap.isOpened():
                raise RuntimeError(f"Камера {cfg['camera_idx']} недоступна")
            # ── Применяем настройки камеры (п.1 ТЗ) ──────────────────
            cam_w   = cfg.get("cam_width",  0)
            cam_h   = cfg.get("cam_height", 0)
            cam_fps = cfg.get("cam_fps_set", 0)
            if cam_w > 0 and cam_h > 0:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cam_w)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
                print(f"[CAM] Установлено разрешение: {cam_w}×{cam_h}")
            if cam_fps > 0:
                self.cap.set(cv2.CAP_PROP_FPS, cam_fps)
                print(f"[CAM] Установлен FPS: {cam_fps}")
            self.total=0; self.is_file=False
            self.fps_actual = self.cap.get(cv2.CAP_PROP_FPS)
            if self.fps_actual <= 0: self.fps_actual = float(cam_fps) if cam_fps > 0 else 25.0
            self.res_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.res_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Строка "разрешение" для CSV (п.1 ТЗ)
        self.source_res = f"{self.res_w}x{self.res_h}"

        self.tracker = BubbleTracker(
            min_track_frames = cfg.get("min_track_frames", 3),
            px_per_mm = cfg.get("px_per_mm", 10.0),
            fps = self.fps_actual,
            enable_per_frame_log = cfg.get("save_per_frame", True)
        )


        r = cfg["roi"]
        self.roi = (r["x"],r["y"],r["w"],r["h"]) if r["w"]>0 and r["h"]>0 else None

        # ── pipeline init ─────────────────────────────────────────
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        ksize = max(cfg["max_radius"], 9) | 1
        self._tk  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
        self._mk  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

        self.bg_sub = None
        if cfg.get("use_mog2", True):
            self.bg_sub = cv2.createBackgroundSubtractorMOG2(
                history=cfg.get("mog2_history", 200),
                varThreshold=cfg.get("mog2_thresh", 16),
                detectShadows=False)

        bp = cv2.SimpleBlobDetector_Params()
        bp.filterByArea = True
        bp.minArea = math.pi * max(1, cfg["min_radius"])**2 * 0.4
        bp.maxArea = math.pi * cfg["max_radius"]**2 * 3.0
        bp.filterByCircularity = True
        bp.minCircularity = max(0.2, cfg.get("min_circularity", 0.4) - 0.15)
        bp.filterByConvexity = True; bp.minConvexity = 0.55
        bp.filterByInertia = True;   bp.minInertiaRatio = 0.25
        self.blob_det = cv2.SimpleBlobDetector_create(bp)

        self.use_of    = cfg.get("use_of", False)
        self.prev_gray = None

        # ── GUI-переменные (заменяют cv2.trackbar) ────────────────
        # Читаются в worker thread, пишутся из GUI thread (int — атомарно)
        self._delay_var    = tk.IntVar(value=cfg["delay_ms"])
        self._param2_var   = tk.IntVar(value=cfg["param2"])
        self._line_pct_var = tk.IntVar(value=cfg["line_pct"])
        self._speed_var    = tk.IntVar(value=3)
        self._seek_var     = tk.IntVar(value=0)

        # ── Очереди для потокобезопасной передачи данных ──────────
        # _frame_q: worker → GUI (кадр BGR + stats dict)
        # _cmd_q:   GUI → worker (строки: "pause","resume","reset","stop","next","back")
        self._frame_q  = _queue_mod.Queue(maxsize=2)
        self._cmd_q    = _queue_mod.Queue()
        self._worker_thread = None

        # ── Живой график (буфер истории) ──────────────────────────
        self._chart_speed_hist  = deque(maxlen=cfg.get("chart_width", 120))
        self._chart_radius_hist = deque(maxlen=cfg.get("chart_width", 120))
        # ── Кеш оверлея (фикс мигания) ────────────────────────
        self._cached_chart_overlay = None   # numpy BGRA или None
        self._chart_dirty          = True   # сброс при новых данных
        self._cached_alarms        = []     # список строк тревог
        self._alarms_frame_counter = 0      # пересчитываем не каждый кадр

        # ── Запись видео ──────────────────────────────────────────
        self._video_writer = None
        if cfg.get("record_video", False):
            safe_t = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            rec_path = str(OUT / f"annotated_{safe_t}.mp4")
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            # размер узнаем после первого кадра, инициализируем лениво
            self._rec_path  = rec_path
            self._rec_fps   = cfg.get("rec_fps", 25)
            self._rec_size  = None  # (w, h) — заполнится при первом кадре
            print(f"[REC] Запись активирована: {rec_path}")
        else:
            self._rec_path = None

        # ── Хранилище последних активных пузырьков для тревог ─────
        self._alarm_history = deque(maxlen=60)

        # ── Live Dashboard (через Toplevel поверх единого app_root) ─────────
        self.dashboard = None
        if self._app_root:
            try:
                self.dashboard = LiveDashboard(
                    tracker     = self.tracker,
                    cfg         = cfg,
                    fps_actual  = self.fps_actual,
                    res_w       = self.res_w,
                    res_h       = self.res_h,
                    tracker_app = self,   # для cmd_q + IntVar слайдеров
                )
            except Exception as e:
                print(f"[DASHBOARD] Ошибка: {e}")
                self.dashboard = None

    def _seek(self, v):
        """GUI-thread: запоминаем цель seek (только последнее значение)."""
        if self.is_file:
            self._seek_var.set(int(float(v)))
            self._seek_target = int(float(v))   # атомарная запись (GIL)

    def _get_line_y(self, frame) -> int:
        """Абсолютный Y счётной линии в координатах кадра."""
        pct = self._line_pct_var.get() / 100.0
        return max(1, int(frame.shape[0] * pct))

    # ── ROI через Canvas дашборда ──────────────────────────────────
    def _canvas_mouse_down(self, x, y):
        self._drawing = True; self._roi_start = (x, y)

    def _canvas_mouse_move(self, x, y):
        if self._drawing:
            self._tmp_roi = (self._roi_start[0], self._roi_start[1],
                             x - self._roi_start[0], y - self._roi_start[1])

    def _canvas_mouse_up(self, x, y):
        self._drawing = False
        if self._tmp_roi:
            rx, ry, rw, rh = self._tmp_roi
            if abs(rw) > 10 and abs(rh) > 10:
                if rw < 0: rx, rw = rx + rw, -rw
                if rh < 0: ry, rh = ry + rh, -rh
                self.roi = (rx, ry, rw, rh)
        self._tmp_roi = None

    def _canvas_mouse_right(self):
        """Сброс ROI по правой кнопке мыши."""
        self.roi = None

    # ═══════════════════════════════════════════════════════════════
    #  DETECTION PIPELINE
    # ═══════════════════════════════════════════════════════════════
    def _preprocess(self, gray):
        eq     = self.clahe.apply(gray)
        tophat = cv2.morphologyEx(eq, cv2.MORPH_TOPHAT, self._tk)
        enh    = cv2.addWeighted(eq, 0.65, tophat, 0.35, 0)
        return enh, tophat

    def _fg_mask(self, gray):
        if self.bg_sub is None: return None
        m = self.bg_sub.apply(gray)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self._mk)
        m = cv2.dilate(m, self._mk, iterations=3)
        return m

    def _detect_hough(self, img, p2):
        blurred = cv2.medianBlur(img, 5)
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1,
            minDist   = max(8, self.cfg["min_radius"]*2),
            param1    = 80,
            param2    = p2,
            minRadius = self.cfg["min_radius"],
            maxRadius = self.cfg["max_radius"])
        if circles is None: return []
        return [(int(cx),int(cy),int(cr), float(cr), float(cr), 0.0)
                for cx,cy,cr in np.uint16(np.around(circles))[0]]

    # ── Convexity Defects: разделение двух слипшихся пузырьков ─────────
    def _split_by_convexity(self, cnt, mina, maxa):
        """Возвращает список контуров после разделения по точкам вогнутости."""
        if len(cnt) < 10: return [cnt]
        hull_idx = cv2.convexHull(cnt, returnPoints=False)
        if hull_idx is None or len(hull_idx) < 3: return [cnt]
        try:
            defects = cv2.convexityDefects(cnt, hull_idx)
        except cv2.error:
            return [cnt]
        if defects is None: return [cnt]

        area = cv2.contourArea(cnt)
        # Порог глубины: 15% от sqrt(area) — настраивается через cfg
        depth_thresh = math.sqrt(area) * self.cfg.get("convex_depth_ratio", 0.15) * 256
        deep = []
        for d in defects:
            s, e, f, depth = d[0]
            if depth > depth_thresh:
                deep.append(f)  # индекс точки максимального дефекта

        if len(deep) < 2: return [cnt]  # нет явных слипаний

        # Берём две самые глубокие точки как линию разреза
        deep.sort()
        pt1_idx, pt2_idx = deep[0], deep[-1]
        part_a = np.concatenate([cnt[pt1_idx:pt2_idx+1]])
        part_b = np.concatenate([cnt[pt2_idx:], cnt[:pt1_idx+1]])
        result = []
        for part in [part_a, part_b]:
            if len(part) < 5: continue
            a = cv2.contourArea(part)
            if mina <= a <= maxa:
                result.append(part)
        return result if result else [cnt]

    # ── Enhanced Watershed: разделение кластеров ─────────────────────
    def _split_by_watershed(self, mask_region, offset_x, offset_y, mina, maxa):
        """Делит бинарную маску через Distance Transform + Watershed.
        Возвращает список (cx, cy, r, amin, amax, angle)."""
        try:
            from scipy.ndimage import distance_transform_edt, label as ndi_label
            from skimage.feature import peak_local_max
            from skimage.segmentation import watershed as sk_watershed
        except ImportError:
            return []

        dist = distance_transform_edt(mask_region)
        # Находим пики — центры отдельных пузырьков
        min_dist_px = max(self.cfg["min_radius"], 5)
        peaks = peak_local_max(dist, min_distance=min_dist_px, exclude_border=False)
        if len(peaks) < 2: return []  # только один пик — не делим

        markers = np.zeros_like(dist, dtype=np.int32)
        for i, (py, px) in enumerate(peaks):
            markers[py, px] = i + 1
        labels = sk_watershed(-dist, markers, mask=mask_region)

        result = []
        for lbl_id in range(1, len(peaks) + 1):
            region = np.uint8(labels == lbl_id) * 255
            cnts, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                a = cv2.contourArea(c)
                if a < mina or a > maxa: continue
                if len(c) >= 5:
                    (cx, cy), (ma, MA), angle = cv2.fitEllipse(c)
                    cr = (ma + MA) / 4.0
                else:
                    (cx, cy), cr = cv2.minEnclosingCircle(c)
                    ma, MA, angle = cr*2, cr*2, 0.0
                if self.cfg["min_radius"] <= int(cr) <= self.cfg["max_radius"]:
                    result.append((int(cx + offset_x), int(cy + offset_y),
                                   int(cr), float(ma/2), float(MA/2), float(angle)))
        return result

    def _detect_contours(self, tophat):
        blr=cv2.GaussianBlur(tophat,(5,5),0)
        _,thr=cv2.threshold(blr,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        thr=cv2.morphologyEx(thr,cv2.MORPH_OPEN,self._mk)
        cnts,_=cv2.findContours(thr,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        res=[]
        mina=math.pi*self.cfg["min_radius"]**2*0.35
        # Пороговая площадь для запуска сегментации (2 пузырька min_radius)
        split_area = math.pi*(self.cfg["min_radius"]*1.8)**2
        maxa=math.pi*self.cfg["max_radius"]**2*3.0
        minc=self.cfg.get("min_circularity",0.40)
        # Отключил тяжелые алгоритмы разделения по умолчанию (снижает нагрузку)
        use_conv = self.cfg.get("use_convexity_split", False)
        use_ws   = self.cfg.get("use_watershed", False)

        for cnt in cnts:
            area=cv2.contourArea(cnt)
            if area<mina or area>maxa: continue
            peri=cv2.arcLength(cnt,True)
            if peri<1: continue
            circ=4*math.pi*area/(peri*peri)

            # ── Крупный контур? Пробуем разделить ────────────────────
            split_done = False  # init before if-block
            if area > split_area and circ < 0.75:
                # Метод 1: Convexity Defects
                if use_conv:
                    parts = self._split_by_convexity(cnt, mina, maxa)
                    if len(parts) > 1:
                        split_done = True
                        for part in parts:
                            if len(part) >= 5:
                                (cx2,cy2),(ma2,MA2),ang2 = cv2.fitEllipse(part)
                                cr2 = (ma2+MA2)/4.0
                            else:
                                (cx2,cy2),cr2 = cv2.minEnclosingCircle(part)
                                ma2,MA2,ang2 = cr2*2, cr2*2, 0.0
                            if self.cfg["min_radius"]<=int(cr2)<=self.cfg["max_radius"]:
                                res.append((int(cx2), int(cy2), int(cr2), float(ma2/2), float(MA2/2), float(ang2)))

            # Добавляем стандартно если не разделили
            if not split_done:
                if len(cnt) >= 5:
                    (cx,cy),(ma,MA),angle = cv2.fitEllipse(cnt)
                    cr = (ma+MA)/4.0
                else:
                    (cx,cy),cr = cv2.minEnclosingCircle(cnt)
                    ma,MA,angle = cr*2, cr*2, 0.0
                if self.cfg["min_radius"] <= int(cr) <= self.cfg["max_radius"]:
                    res.append((int(cx),int(cy),int(cr),float(ma/2),float(MA/2),float(angle)))
        return res

    def _of_verify(self, curr_gray, active_pts):
        pts = list(active_pts.items())
        if not pts or self.prev_gray is None: return set()
        try:
            ids   = [p[0] for p in pts]
            # p[1] is (x, y) — take exactly 2 floats
            coords = [(float(p[1][0]), float(p[1][1])) for p in pts]
            prev_p = np.array(coords, np.float32).reshape(-1, 1, 2)
            next_p, status, _ = cv2.calcOpticalFlowPyrLK(
                self.prev_gray, curr_gray, prev_p, None,
                winSize=(21, 21), maxLevel=3)
            confirmed = set()
            for i, (oid, st, npt) in enumerate(zip(ids, status.flatten(), next_p)):
                if st == 1:
                    if prev_p[i][0][1] - npt[0][1] > 0.1:
                        confirmed.add(oid)
                else:
                    confirmed.add(oid)
            return confirmed
        except Exception:
            return set(active_pts.keys())  # при ошибке пропускаем все

    def _detect_blobs(self, img):
        """SimpleBlobDetector \u2014 \u0442\u0440\u0435\u0442\u0438\u0439 \u043f\u0440\u043e\u0445\u043e\u0434 \u0434\u0435\u0442\u0435\u043a\u0446\u0438\u0438 (\u0434\u043e\u043f\u043e\u043b\u043d\u044f\u0435\u0442 Hough \u0438 contours)."""
        res = []
        try:
            kps = self.blob_det.detect(img)
            for kp in kps:
                x, y = int(kp.pt[0]), int(kp.pt[1])
                r = int(kp.size / 2)
                if self.cfg["min_radius"] <= r <= self.cfg["max_radius"]:
                    res.append((x, y, r, float(r), float(r), 0.0))
        except Exception:
            pass
        return res

    def _detect_all(self, gray_small, scale):
        p2 = max(5, self._param2_var.get())   # читаем безопасно из worker thread
        enh,tophat=self._preprocess(gray_small)
        mask=self._fg_mask(gray_small)

        if mask is not None:
            det_in =cv2.bitwise_and(enh,   enh,   mask=mask)
            top_in =cv2.bitwise_and(tophat, tophat, mask=mask)
        else:
            det_in=enh; top_in=tophat

        dh = [] # Отключено для оптимизации: self._detect_hough(det_in,p2)
        dc = self._detect_contours(top_in)
        db = [] # Отключено для оптимизации: self._detect_blobs(det_in)
        
        counts={"hough":len(dh),"contour":len(dc),"blob":len(db)}
        merged = dh + dc + db
        scaled=[(int(x/scale),int(y/scale),int(r/scale), a_min/scale, a_max/scale, angle) 
                for x,y,r,a_min,a_max,angle in merged]
        return scaled,counts

    # ═══════════════════════════════════════════════════════════════
    #  RENDERING
    # ═══════════════════════════════════════════════════════════════
    def _draw_counting_line(self, frame, line_y, roi_offset_y=0):
        """Рисуем счётную линию и метку."""
        fy = line_y + roi_offset_y   # в координатах кадра
        h, w = frame.shape[:2]
        fy = max(2, min(fy, h-2))

        # анимированная линия (мигает каждые 15 кадров)
        alpha = 0.85 + 0.15*math.sin(self.frame_n/8)
        col = (int(0*alpha), int(255*alpha), int(220*alpha))

        cv2.line(frame, (0,fy), (w,fy), col, 2, cv2.LINE_AA)

        # метка
        cv2.rectangle(frame,(0,fy-18),(130,fy-1),(0,0,0),-1)
        cv2.putText(frame,"COUNT LINE",
                    (4,fy-4),cv2.FONT_HERSHEY_SIMPLEX,0.48,col,1,cv2.LINE_AA)

        # стрелки направления по краям
        arr_y = fy+20
        if arr_y < h-5:
            for ax in [w//4, w//2, 3*w//4]:
                cv2.arrowedLine(frame,(ax,arr_y),(ax,fy+4),
                                (0,180,100),1,cv2.LINE_AA,tipLength=0.4)

    def _draw_bubbles(self, frame, active, roi_offset_x=0, roi_offset_y=0):
        for oid,(bx,by) in active.items():
            # Если пузырёк физически не найден в этом кадре — не рисуем его (убирает зависания)
            if self.tracker.disappeared.get(oid, 0) > 0:
                continue

            lr = int(list(self.tracker.radii.get(oid,[10]))[-1])
            spd= self.tracker.current_speed(oid)
            age= self.tracker.age.get(oid,0)

            if oid in self.tracker.counted:
                # уже подсчитан — ярко-зелёный, двойной круг
                col=(0,230,80)
                cv2.circle(frame,(bx,by),lr,col,2,cv2.LINE_AA)
                cv2.circle(frame,(bx,by),lr+3,col,1,cv2.LINE_AA)
                cv2.circle(frame,(bx,by),3,col,-1)
                # Убрали символ '✓', так как OpenCV шрифты не поддерживают Unicode и рисуют '???'
                cv2.putText(frame,f"#{oid} v={spd:.1f}",
                            (bx-28,by-lr-6),
                            cv2.FONT_HERSHEY_SIMPLEX,0.38,col,1,cv2.LINE_AA)
            elif age >= self.tracker.min_track_frames:
                # взрослый трек — готов к подсчёту, циан
                col=(0,210,255)
                cv2.circle(frame,(bx,by),lr,col,2,cv2.LINE_AA)
                cv2.circle(frame,(bx,by),3,col,-1)
                cv2.putText(frame,f"#{oid} v={spd:.1f}",
                            (bx-24,by-lr-6),
                            cv2.FONT_HERSHEY_SIMPLEX,0.36,col,1,cv2.LINE_AA)
            else:
                # молодой трек — серый, набирает возраст
                g=int(80+100*(age/max(self.tracker.min_track_frames,1)))
                cv2.circle(frame,(bx,by),lr,(g,g,g),1,cv2.LINE_AA)

    def _hud(self, frame, elapsed, counts, line_y):
        h,w=frame.shape[:2]
        total  =self.tracker.total
        on_scr =self.tracker.active_count
        avg_r  =self.tracker.avg_radius()
        rate   =self.tracker.rate(elapsed)
        n_zones=len([z for z in self.tracker._exit_zones
                      if z[3]>self.tracker.frame_n])

        # ── Публикация кадра в GUI (queue) и веб-стрим ───────────────────────
        # 1. Кладём кадр в очередь для LiveDashboard (non-blocking)
        if self.dashboard:
            try: self._frame_q.put_nowait(frame.copy())
            except _queue_mod.Full: pass
        self._last_frame = frame.copy()

        # ROI overlay (единственный overlay на кадре)

        if self.roi:
            rx,ry,rw,rh=self.roi
            cv2.rectangle(frame,(rx,ry),(rx+rw,ry+rh),(80,255,255),2)
            cv2.putText(frame,"ROI",(rx,max(4,ry-6)),
                        cv2.FONT_HERSHEY_SIMPLEX,0.45,(80,255,255),1,cv2.LINE_AA)
        if self._tmp_roi:
            tx,ty,tw,th2=self._tmp_roi
            cv2.rectangle(frame,(tx,ty),(tx+tw,ty+th2),(255,200,0),1)

    # ═══════════════════════════════════════════════════════════════
    #  MAIN LOOP  (threading архитектура)
    # ═══════════════════════════════════════════════════════════════
    def run(self, app_root=None):
        """
        Запуск: CV-обработка → отдельный daemon thread (worker).
        GUI thread только рендерит кадры из очереди каждые 16 мс.
        Благодаря этому перетаскивание окна НЕ останавливает видео.
        """
        self._tk_root = app_root or self._app_root
        self._scale   = self.cfg["scale"]
        self._skip    = self.cfg["skip_frames"]
        self._running = True

        print("\n" + "-"*62)
        print("  BUBBLE TRACKER PRO v6.0 — threading mode")
        print("-"*62 + "\n")

        # Запускаем CV-поток (daemon → умирает вместе с main)
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="CVWorker")
        self._worker_thread.start()

        if self._tk_root:
            # GUI: опрашивает очередь каждые 16 мс (~60 fps)
            self._tk_root.after(16, self._poll_frame)
        else:
            # Fallback без Tkinter: ждём завершения worker
            self._worker_thread.join()

    def _worker_loop(self):
        """CV-поток: читает кадры, детектирует, трекает, пишет в очередь."""
        try:
            while self._running:
                delay = self._process_one_frame()
                if delay == -1:
                    self._running = False
                    self._finish()
                    # Сигнализируем GUI об остановке
                    if self._tk_root:
                        try: self._tk_root.after(0, self._tk_root.destroy)
                        except: pass
                    break
                if delay > 1:
                    time.sleep(delay / 1000.0)
        except Exception as e:
            print(f"[CVWorker ERROR] {e}")
            self._running = False

    def _poll_frame(self):
        """GUI-поток: забирает кадр из очереди и показывает в дашборде."""
        if not self._running:
            return
        try:
            frame_bgr = self._frame_q.get_nowait()
            # Передаём кадр в дашборд для отображения
            if self.dashboard and self.dashboard._alive:
                self.dashboard.show_frame(frame_bgr)
        except _queue_mod.Empty:
            pass
        # Планируем следующий вызов — mainloop продолжает работать
        if self._tk_root and self._running:
            self._tk_root.after(16, self._poll_frame)

    def _process_one_frame(self) -> int:
        """
        Обрабатывает один кадр.
        Возвращает: кол-во мс до следующего кадра, или -1 для остановки.
        """
        scale = self._scale
        skip  = self._skip

        # ── ПАУЗА: команды из _cmd_q (вместо cv2.waitKey) ──────────
        if self._paused:
            try:
                cmd = self._cmd_q.get_nowait()
                if cmd == "stop":   return -1
                if cmd == "resume": self._paused = False
                elif cmd == "next" and self.is_file:
                    ret2, frame2 = self.cap.read()
                    if ret2:
                        self._last_frame = frame2.copy()
                        if self.total > 0:
                            self._seek_var.set(min(int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)), self.total))
                        line_y = self._get_line_y(frame2)
                        self._hud(frame2, time.time()-self.t0, self._det_counts, line_y)
                        self._draw_counting_line(frame2, line_y)
                        if self.dashboard:
                            try: self._frame_q.put_nowait(frame2.copy())
                            except _queue_mod.Full: pass
                elif cmd == "back" and self.is_file:
                    pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, pos - 31))
                    ret2, frame2 = self.cap.read()
                    if ret2:
                        self._last_frame = frame2.copy()
                        self._seek_var.set(min(int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)), self.total))
                        line_y = self._get_line_y(frame2)
                        self._hud(frame2, time.time()-self.t0, self._det_counts, line_y)
                        self._draw_counting_line(frame2, line_y)
                        if self.dashboard:
                            try: self._frame_q.put_nowait(frame2.copy())
                            except _queue_mod.Full: pass
            except _queue_mod.Empty:
                pass
            return 50  # пауза — проверяем каждые 50 мс

        # ── ЧТЕНИЕ КАДРА ───────────────────────────────────────────
        ret, frame = self.cap.read()
        if not ret:
            if self.is_file:
                return -1   # видео закончилось
            else:
                return 10   # камера — просто подождать

        self.frame_n += 1
        if self.is_file and self.total > 0:
            pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            self._seek_var.set(min(pos, self.total))  # обновляем ползунок позиции

        # Пропуск кадров (подаём в очередь без обработки)
        if self.frame_n % skip != 0:
            if self.dashboard:
                try: self._frame_q.put_nowait(frame.copy())
                except _queue_mod.Full: pass
            return 1

        # ── ROI crop ───────────────────────────────────────────────
        if self.roi:
            rx,ry,rw,rh = self.roi
            fh,fw = frame.shape[:2]
            rx=max(0,min(rx,fw-1)); ry=max(0,min(ry,fh-1))
            rw=min(rw,fw-rx);       rh=min(rh,fh-ry)
            crop=frame[ry:ry+rh,rx:rx+rw]; ox,oy=rx,ry
        else:
            crop=frame; ox=oy=0

        # ── масштаб ────────────────────────────────────────────────
        sw2=max(1,int(crop.shape[1]*scale))
        sh2=max(1,int(crop.shape[0]*scale))
        small=cv2.resize(crop,(sw2,sh2))
        gray =cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)

        # ── детекция ───────────────────────────────────────────────
        dets_roi,counts=self._detect_all(gray,scale)
        self._det_counts=counts
        dets=[(x+ox,y+oy,r,a_min,a_max,angle)
              for x,y,r,a_min,a_max,angle in dets_roi]

        # ── optical flow ────────────────────────────────────────────
        cg=cv2.cvtColor(cv2.resize(frame,(sw2,sh2)),cv2.COLOR_BGR2GRAY)
        of_ok=self._of_verify(cg,dict(self.tracker.positions))
        self.prev_gray=cg

        # ── счётная линия ───────────────────────────────────────────
        line_y=self._get_line_y(frame)
        if self.roi:
            lypct = self._line_pct_var.get() / 100.0
            counting_line_abs=oy+int(rh*lypct)
        else:
            counting_line_abs=line_y

        # ── трекер ──────────────────────────────────────────────────
        active_all=self.tracker.update(dets,counting_line_abs,gray)
        active=(
            {oid:p for oid,p in active_all.items() if oid in of_ok}
            if self.use_of else active_all
        )
        elapsed=time.time()-self.t0

        # ── отрисовка ───────────────────────────────────────────────
        self._draw_bubbles(frame,active_all)
        self._draw_counting_line(frame,counting_line_abs)
        self._hud(frame,elapsed,counts,counting_line_abs)

        # История для фронтенд-графика (оверлей удалён, данные идут через WS)
        if active_all:
            speeds=[abs(self.tracker.velocities.get(oid,(0,0))[1]) for oid in active_all]
            radii =[list(self.tracker.radii.get(oid,[0]))[-1]
                    for oid in active_all if self.tracker.radii.get(oid)]
            new_sp = sum(speeds)/len(speeds) if speeds else 0
            new_r  = sum(radii)/len(radii)  if radii  else 0
            self._chart_speed_hist.append(new_sp)
            self._chart_radius_hist.append(new_r)
            # _draw_live_chart УДАЛЁН — график на фронтенде

        # Тревоги пересчитываем раз в 30 кадров (не каждый кадр)
        self._alarms_frame_counter += 1
        if self._alarms_frame_counter >= 30:
            self._alarms_frame_counter = 0
            self._cached_alarms = self._compute_alarms(active_all)
        self._draw_alarms_cached(frame)

        # ── Публикация в веб-стрим (если API запущен) ──────────────
        try:
            from main import frame_publisher, live_stats as _ls, tracker_commands
            ok2, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok2: frame_publisher.push_frame(jpeg.tobytes())
            _el = time.time() - self.t0
            _ls.update({"active": True, "total": self.tracker.total,
                        "on_screen": self.tracker.active_count,
                        "rate": round(self.tracker.rate(_el), 2),
                        "elapsed": round(_el, 1),
                        "avg_radius": self.tracker.avg_radius(),
                        "source": os.path.basename(self.cfg.get("video_path","") or f"Cam{self.cfg.get('camera_idx',0)}"),
                        "paused": self._paused, "frame_n": self.frame_n,
                        # Данные для React-графика (последние 40 точек)
                        "speed_hist":  list(self._chart_speed_hist)[-40:],
                        "radius_hist": list(self._chart_radius_hist)[-40:]})
            if tracker_commands.get("reset"):
                tracker_commands["reset"] = False; self._reset()
            if tracker_commands.get("stop"):
                tracker_commands["stop"] = False; return -1
            if tracker_commands.get("pause") and not self._paused: self._paused = True
            elif not tracker_commands.get("pause") and self._paused: self._paused = False
            new_line = tracker_commands.get("line_y_pct")
            if new_line is not None:
                tracker_commands["line_y_pct"] = None
                self._line_pct_var.set(int(new_line))
        except ImportError:
            pass

        # ── seek (атомарная переменная, не очередь) ─────────────────
        tgt = self._seek_target
        if tgt is not None:
            self._seek_target = None   # сбрасываем ДО cap.set чтобы не повторить
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, tgt)
            # Сбрасываем очередь кадров — старые кадры уже не нужны
            while not self._frame_q.empty():
                try: self._frame_q.get_nowait()
                except: break

        # ── команды из _cmd_q (GUI → worker) ────────────────────────
        try:
            cmd = self._cmd_q.get_nowait()
            if cmd == "stop":    return -1
            if cmd == "pause":   self._paused = True
            if cmd == "resume":  self._paused = False
            if cmd == "reset":   self._reset()
            if cmd == "back":
                pos = max(0, self.cap.get(cv2.CAP_PROP_POS_FRAMES) - 30)
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            if cmd == "next":
                pos = self.cap.get(cv2.CAP_PROP_POS_FRAMES) + 1
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        except _queue_mod.Empty:
            pass

        # ── кадр → очередь GUI ──────────────────────────────────────
        self._last_frame = frame.copy()
        try: self._frame_q.put_nowait(frame.copy())
        except _queue_mod.Full: pass

        # ── запись видео ─────────────────────────────────────────────
        if self._rec_path:
            h_f, w_f = frame.shape[:2]
            if self._video_writer is None:
                self._video_writer = cv2.VideoWriter(
                    self._rec_path, cv2.VideoWriter_fourcc(*'mp4v'),
                    self._rec_fps, (w_f, h_f))
            self._video_writer.write(frame)

        # ── скорость из IntVar ───────────────────────────────────────
        delay  = self._delay_var.get()
        spd_i  = max(1, self._speed_var.get())
        extra  = {1: 160, 2: 60, 3: 0, 4: -1}[spd_i]
        total_delay = max(1, delay + max(0, extra))
        if spd_i == 4 and self.is_file: self.cap.read()
        return total_delay
    def _card(self, parent, title, title_color=None):
        """Карточка-контейнер."""
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="x", pady=(0,6))
        tk.Label(f, text=title, font=self.FONT_TITLE,
                 bg=self.BG2, fg=title_color or self.FG,
                 anchor="w").pack(fill="x", padx=10, pady=(6,2))
        return f


    def _run_blocking(self):
        """Fallback: классический цикл (без дашборда/app_root)."""
        try:
            while True:
                d = self._process_one_frame()
                if d == -1:
                    break
                if d > 1:
                    time.sleep(d / 1000.0)
        finally:
            self._finish()

    def _get_line_y(self, frame) -> int:
        """Y счётной линии. Читаем _line_pct_var (безопасно из любого потока)."""
        pct = self._line_pct_var.get() / 100.0
        return max(1,int(frame.shape[0]*pct))

    # ── Живой мини-график (осциллоскоп скорости/размера) ──────────
    def _draw_live_chart(self, frame):
        """Producer-Consumer модель: обновляем кеш только при новых данных,
        накладываем кеш на каждом кадре без перерасчёта."""
        h, w = frame.shape[:2]
        cw = min(280, w // 3)
        ch = 80
        cx0, cy0 = w - cw - 10, 10

        # Перестраиваем кеш только если данные изменились
        if self._chart_dirty or self._cached_chart_overlay is None:
            self._chart_dirty = False
            # Рисуем целиком на отдельном холсте (нет frame.copy()!)
            canvas = np.zeros((ch + 8, cw + 8, 3), np.uint8)
            cv2.rectangle(canvas, (0, 0), (cw+7, ch+7), (20, 20, 40), -1)
            cv2.rectangle(canvas, (0, 0), (cw+7, ch+7), (60, 60, 100), 1)

            def norm_series(data, color, offset_y=0):
                arr = list(data)
                if len(arr) < 2: return
                mx = max(arr) or 1
                pts = []
                for i, v in enumerate(arr):
                    px = 4 + int(i * cw / len(arr))
                    py = ch - offset_y - int(v / mx * (ch // 2 - 4))
                    pts.append((px, py))
                for i in range(1, len(pts)):
                    cv2.line(canvas, pts[i-1], pts[i], color, 1, cv2.LINE_AA)

            norm_series(self._chart_speed_hist,  (0, 200, 255), 0)
            norm_series(self._chart_radius_hist, (80, 255, 120), ch // 2)
            cv2.putText(canvas, "v",    (4,  14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0,200,255),  1)
            cv2.putText(canvas, "r",    (4,  ch//2+14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (80,255,120), 1)
            cv2.putText(canvas, "LIVE", (cw-25, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (150,150,200), 1)
            self._cached_chart_overlay = canvas

        # Накладываем кеш на кадр (дешево: нет copy+addWeighted)
        ov = self._cached_chart_overlay
        oy, ox = ov.shape[:2]
        roi = frame[cy0:cy0+oy, cx0:cx0+ox]
        if roi.shape == ov.shape:
            cv2.addWeighted(ov, 0.7, roi, 0.3, 0, roi)

    def _compute_alarms(self, active_all) -> list:
        """Producer: вычисляем тревоги раз в 30 кадров, не каждый."""
        alarms = []
        alarm_re  = self.cfg.get("alarm_re", 0)
        alarm_big = self.cfg.get("alarm_big_pct", 0)
        alarm_min = self.cfg.get("alarm_min_idx", 0)
        log = self.tracker.detailed_log
        if alarm_re > 0 and log:
            recent_re = [r.get("reynolds", 0) for r in log[-20:]]
            avg_re = sum(recent_re) / len(recent_re) if recent_re else 0
            if avg_re > alarm_re:
                alarms.append(f"TURBULENTNOST! Re={avg_re:.0f}>{alarm_re}")
        if alarm_big > 0 and active_all:
            radii = [list(self.tracker.radii.get(oid,[0]))[-1] for oid in active_all
                     if self.tracker.radii.get(oid)]
            if radii:
                big_pct = sum(1 for r in radii if r > self.cfg.get("max_radius",30)*0.8) / len(radii) * 100
                if big_pct > alarm_big:
                    alarms.append(f"KRUPNYE PUZYRI: {big_pct:.0f}%>{alarm_big}%")
        if alarm_min > 0 and self.tracker.active_count < alarm_min:
            alarms.append(f"MALO PUZYRKOV: {self.tracker.active_count}<{alarm_min}")
        return alarms

    def _draw_alarms_cached(self, frame):
        """Consumer: рисуем закэшированные тревоги на каждом кадре."""
        if not self._cached_alarms:
            return
        h, w = frame.shape[:2]
        for i, msg in enumerate(self._cached_alarms):
            y = h - 50 - i * 22
            cv2.rectangle(frame, (4, y-16), (4+len(msg)*9, y+4), (20,0,0), -1)
            cv2.putText(frame, msg, (6, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 60, 255), 1, cv2.LINE_AA)


    # ── Умные тревоги ─────────────────────────────────────────────
    def _draw_alarms(self, frame, active_all):
        alarms = []
        alarm_re  = self.cfg.get("alarm_re", 0)
        alarm_big = self.cfg.get("alarm_big_pct", 0)
        alarm_min = self.cfg.get("alarm_min_idx", 0)
        log = self.tracker.detailed_log

        if alarm_re > 0 and log:
            recent_re = [r.get("reynolds", 0) for r in log[-20:]]
            avg_re = sum(recent_re) / len(recent_re) if recent_re else 0
            if avg_re > alarm_re:
                alarms.append(f"TURBULENTNOST! Re={avg_re:.0f} > {alarm_re}")

        if alarm_big > 0 and active_all:
            big = sum(1 for oid in active_all
                       if list(self.tracker.radii.get(oid, [0]))[-1]
                          > self.cfg.get("max_radius", 25) * 0.7)
            pct = big / len(active_all) * 100 if active_all else 0
            if pct > alarm_big:
                alarms.append(f"KRUPNYE PUZYRKI! {pct:.0f}% > {alarm_big}%")

        if alarm_min > 0 and log:
            recent_min = [r.get("mineralization", 200) for r in log[-20:]]
            avg_min = sum(recent_min) / len(recent_min) if recent_min else 200
            if avg_min < alarm_min:
                alarms.append(f"NIZK.MINERALIZACIA! {avg_min:.0f} < {alarm_min}")

        for i, msg in enumerate(alarms):
            y_pos = 80 + i * 28
            overlay = frame.copy()
            h_msg, w_msg = 24, len(msg) * 9 + 12
            cv2.rectangle(overlay, (8, y_pos - 18), (8 + w_msg, y_pos + 6), (0, 0, 160), -1)
            cv2.addWeighted(overlay, 0.75, frame, 0.25, 0, frame)
            cv2.putText(frame, f"!! {msg} !!",
                        (10, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 60, 255), 2, cv2.LINE_AA)

    def _reset(self):
        self.tracker=BubbleTracker(
            min_track_frames=self.cfg.get("min_track_frames",3),
            px_per_mm=self.cfg.get("px_per_mm",10.0)
        )
        if self.bg_sub and self.cfg.get("use_mog2"):
            self.bg_sub=cv2.createBackgroundSubtractorMOG2(
                history=self.cfg.get("mog2_history",200),
                varThreshold=self.cfg.get("mog2_thresh",16),
                detectShadows=False)
        self.t0=time.time(); self.prev_gray=None
        if self.dashboard:
            self.dashboard.tracker = self.tracker
        print("[RESET] Счётчик сброшен")

    def _finish(self):
        elapsed=time.time()-self.t0
        total  =self.tracker.total
        avg_r  =self.tracker.avg_radius()
        rate   =self.tracker.rate(elapsed)
        roi_s  =str(self.roi) if self.roi else "ves kadr"
        src_s  =(os.path.basename(self.cfg["video_path"])
                 if self.cfg["source_type"]=="video"
                 else f"Camera{self.cfg.get('camera_idx',0)}")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session=dict(
            start_time   =ts,
            source       =src_s,
            duration_sec =round(elapsed,1),
            total_bubbles=total,
            avg_radius_px=avg_r,
            rate_per_sec =rate,
            roi          =roi_s,
            res_w        =self.res_w,
            res_h        =self.res_h,
            fps_actual   =self.fps_actual,
            px_per_mm    =self.cfg.get("px_per_mm", 10.0),
            notes        =self.cfg.get("notes",""),
            det_hough    =getattr(self.tracker, "_det_counts_ext", {}).get("hough", 0),
            det_contour  =getattr(self.tracker, "_det_counts_ext", {}).get("contour", 0),
            det_blob     =getattr(self.tracker, "_det_counts_ext", {}).get("blob", 0),
            use_convex   =self.cfg.get("use_convexity_split", True),
            use_watershed=self.cfg.get("use_watershed", True)
        )
        save_session(session)
        px_mm = self.cfg.get("px_per_mm", 10.0)
        # Итоговый CSV: одна строка на пузырёк (при пересечении линии)
        save_detailed_csv(self.tracker.detailed_log, px_mm, ts,
                          source_res=self.source_res, source_fps=self.fps_actual)
        # Покадровый CSV: строка на каждый кадр × каждый пузырёк (все)
        if self.cfg.get("save_per_frame", True):
            save_per_frame_csv(self.tracker.per_frame_log, px_mm, ts,
                               source_res=self.source_res, source_fps=self.fps_actual)


        # ── Тепловая карта ────────────────────────────────────────
        if self.cfg.get("save_heatmap", False) and self._last_frame is not None \
                and self.tracker.detailed_log:
            log = self.tracker.detailed_log
            hframe = self._last_frame.copy()
            heat = np.zeros(hframe.shape[:2], dtype=np.float32)
            rad  = self.cfg.get("heatmap_radius", 20)
            line_y_heat = int(hframe.shape[0] * self.cfg.get("line_pct", 25) / 100)
            for row in log:
                x = int(row.get("x_px", 0))
                cv2.circle(heat, (x, line_y_heat), rad, 1.0, -1)
            heat = cv2.GaussianBlur(heat, (51, 51), 0)
            max_h = heat.max() or 1
            heat_norm = np.uint8(heat / max_h * 255)
            heat_color = cv2.applyColorMap(heat_norm, cv2.COLORMAP_JET)
            overlay_h = cv2.addWeighted(hframe, 0.55, heat_color, 0.45, 0)
            cv2.line(overlay_h, (0, line_y_heat), (overlay_h.shape[1], line_y_heat),
                     (0, 255, 200), 2)
            cv2.putText(overlay_h, "TEPLOVAYA KARTA POTOKA",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            safe_t = ts.replace(":", "-").replace(" ", "_")
            hmap_path = OUT / f"heatmap_{safe_t}.png"
            cv2.imwrite(str(hmap_path), overlay_h)
            print(f"[HEATMAP] {hmap_path}")

        # ── Остановка записи видео ────────────────────────────────
        if self._video_writer is not None:
            self._video_writer.release()
            print(f"[REC] Видео сохранено: {self._rec_path}")

        self.cap.release()
        # Дашборд закрываем, но не главный root
        if self.dashboard: self.dashboard.close()
        print("\n"+"="*62)
        print("  SESSIYA ZAVERSHENA")
        print(f"  Podschitano: {total}  |  Sr.radius: {avg_r} px  |  V sek: {rate}")
        print(f"  Dlitelnost:  {elapsed:.1f} s  |  Fajly: bubble_results/")
        print("="*62+"\n")


# ═══════════════════════════════════════════════════════════════════════════
#  LIVE DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════
class LiveDashboard:
    """Live Tkinter dashboard — обновляется каждые 500 мс через root.after()."""
    BG   = "#000000"; BG2  = "#0a0a0a"; BG3  = "#111111"
    ACC  = "#7C4DFF"; ACC2 = "#00E5FF"; ACC3 = "#FF4081"
    GRN  = "#00E676"; YEL  = "#FFD740"; ORG  = "#FF9100"
    FG   = "#E0E0FF"; DIM  = "#4A4A7A"; RED  = "#FF1744"

    FONT_MONO  = ("Consolas", 9)
    FONT_MONO2 = ("Consolas", 10, "bold")
    FONT_TITLE = ("Segoe UI", 10, "bold")
    FONT_SM    = ("Segoe UI", 8)
    FONT_BIG   = ("Segoe UI", 18, "bold")
    HISTORY    = 90

    def __init__(self, tracker, cfg, fps_actual, res_w, res_h, tracker_app=None):
        self.tracker     = tracker
        self.cfg         = cfg
        self.fps_actual  = fps_actual
        self.res_w       = res_w
        self.res_h       = res_h
        self._app        = tracker_app  # ссылка на BubbleTrackerApp для cmd_q
        self._alive      = True
        self._t0         = time.time()
        self._speed_hist  = deque([0.0]*self.HISTORY, maxlen=self.HISTORY)
        self._radius_hist = deque([0.0]*self.HISTORY, maxlen=self.HISTORY)
        self._rate_hist   = deque([0.0]*self.HISTORY, maxlen=self.HISTORY)
        self._re_hist     = deque([0.0]*self.HISTORY, maxlen=self.HISTORY)
        self._photo       = None  # храним PhotoImage (чтобы GC не удалил)
        self._build_window()
        self._schedule_update()

    # ── построение окна ──────────────────────────────────────────────
    def _build_window(self):
        self.root = tk.Toplevel()
        self.root.title("Bubble Tracker PRO v5.0 — Live Dashboard")
        self.root.configure(bg=self.BG)
        self.root.geometry("1340x820")
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.root.bind_all('<Escape>', lambda e: self._send_cmd('stop'))
        self.root.bind_all('<space>',  lambda e: self._toggle_pause())
        self.root.bind_all('r',        lambda e: self._send_cmd('reset'))
        self.root.bind_all('R',        lambda e: self._send_cmd('reset'))
        self.root.bind_all('n',        lambda e: self._send_cmd('next'))
        self.root.bind_all('b',        lambda e: self._send_cmd('back'))

        # ── HEADER ──────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=self.BG2, pady=0)
        hdr.pack(fill="x")
        # top accent line
        tk.Frame(hdr, bg=self.ACC2, height=2).pack(fill="x")

        hdr_inner = tk.Frame(hdr, bg=self.BG2)
        hdr_inner.pack(fill="x", padx=12, pady=6)

        # left: title + clock
        left = tk.Frame(hdr_inner, bg=self.BG2)
        left.pack(side="left")
        tk.Label(left, text="⬡  BUBBLE TRACKER PRO v5.0",
                 font=("Segoe UI", 13, "bold"), bg=self.BG2, fg=self.ACC2
                 ).pack(anchor="w")
        self._lbl_clock = tk.Label(left, text="", font=self.FONT_MONO,
                                   bg=self.BG2, fg=self.DIM)
        self._lbl_clock.pack(anchor="w")

        # separator
        tk.Frame(hdr_inner, bg=self.DIM, width=1).pack(side="left", fill="y", padx=18)

        # stat columns
        self._kpi = {}
        self._hdr_vars = {}
        stat_defs = [
            ("schitano",  "SCHITANO",    self.GRN,  "—"),
            ("on_screen", "НА ЭКРАНЕ",   self.ACC2, "—"),
            ("radius",    "AVG RADIUS",  self.YEL,  "—"),
            ("rate",      "ПУЗ/СЕК",    self.ACC,  "—"),
            ("speed_mm",  "СК. мм/с",   "#FF7043", "—"),
            ("tracks",    "ТРЕКОВ",      self.DIM,  "—"),
            ("dur",       "ДЛИТ.",       self.DIM,  "—"),
        ]
        for key, label, color, init in stat_defs:
            col = tk.Frame(hdr_inner, bg=self.BG2, padx=14)
            col.pack(side="left")
            v = tk.StringVar(value=init)
            self._hdr_vars[key] = v
            tk.Label(col, textvariable=v,
                     font=("Consolas", 17, "bold"), bg="#0A0A18", fg=color
                     ).pack(anchor="center")
            tk.Label(col, text=label,
                     font=("Consolas", 8), bg="#0A0A18", fg=self.DIM
                     ).pack(anchor="center")
            # vertical divider
            tk.Frame(hdr_inner, bg="#1A1A35", width=1).pack(side="left", fill="y", padx=0)

        # bottom accent line
        tk.Frame(hdr, bg=self.ACC, height=1).pack(fill="x")

        # ── MAIN LAYOUT ─────────────────────────────────────────────
        main = tk.Frame(self.root, bg=self.BG)
        main.pack(fill="both", expand=True, padx=10, pady=(6,0))

        # ── ВИДЕО ПАНЕЛЬ (заменяет cv2.imshow) ──────────────────
        vid_frame = tk.Frame(main, bg="#000010", bd=1, relief="solid")
        vid_frame.pack(side="left", fill="both", expand=True, padx=(0,8))

        self._video_canvas = tk.Canvas(vid_frame, bg="#000010",
                                       highlightthickness=0, cursor="crosshair")
        self._video_canvas.pack(fill="both", expand=True)

        self._video_canvas.bind("<ButtonPress-1>",   self._vc_press)
        self._video_canvas.bind("<B1-Motion>",       self._vc_drag)
        self._video_canvas.bind("<ButtonRelease-1>", self._vc_release)
        self._video_canvas.bind("<ButtonPress-3>",
                                lambda e: self._app and self._app._canvas_mouse_right())

        # ── кнопки управления ─────────────────────────────────────
        ctrl = tk.Frame(vid_frame, bg=self.BG3)
        ctrl.pack(fill="x", pady=(2,0))
        btn_cfg = dict(bg=self.BG2, fg=self.FG, relief="flat",
                       font=self.FONT_SM, padx=8, pady=2)
        self._btn_pause = tk.Button(ctrl, text="⏸ Пауза",
                                    command=self._toggle_pause, **btn_cfg)
        self._btn_pause.pack(side="left", padx=(4,2), pady=2)
        tk.Button(ctrl, text="R Сброс",
                  command=lambda: self._send_cmd('reset'), **btn_cfg).pack(side="left", padx=2)
        tk.Button(ctrl, text="⏮ -30",
                  command=lambda: self._send_cmd('back'),  **btn_cfg).pack(side="left", padx=2)
        tk.Button(ctrl, text="⏭ +1",
                  command=lambda: self._send_cmd('next'),  **btn_cfg).pack(side="left", padx=2)
        tk.Button(ctrl, text="✕ Стоп",
                  command=lambda: self._send_cmd('stop'),
                  bg=self.BG2, relief="flat", font=self.FONT_SM, padx=8, pady=2,
                  fg=self.RED).pack(side="right", padx=(2,4))

        # ── слайдеры (заменяют cv2.createTrackbar) ───────────────
        def _slider(parent, text, var, lo, hi):
            f = tk.Frame(parent, bg=self.BG3)
            f.pack(side="left", fill="x", expand=True, padx=4)
            tk.Label(f, text=text, font=self.FONT_SM, bg=self.BG3,
                     fg=self.DIM, width=10, anchor="w").pack(side="left")
            tk.Scale(f, from_=lo, to=hi, orient="horizontal",
                     variable=var, bg=self.BG3, fg=self.ACC2,
                     highlightthickness=0, troughcolor=self.BG2,
                     length=100, showvalue=True,
                     font=self.FONT_SM).pack(side="left")

        sliders_row = tk.Frame(vid_frame, bg=self.BG3)
        sliders_row.pack(fill="x")
        if self._app:
            _slider(sliders_row, "Zaderjka",  self._app._delay_var,    0, 500)
            _slider(sliders_row, "param2",    self._app._param2_var,   5, 60)
            _slider(sliders_row, "LineY %",   self._app._line_pct_var, 1, 99)
            _slider(sliders_row, "Speed 1-4", self._app._speed_var,    1, 4)
            if self._app.is_file and self._app.total > 0:
                f2 = tk.Frame(vid_frame, bg=self.BG3)
                f2.pack(fill="x", padx=4)
                tk.Label(f2, text="Позиция", font=self.FONT_SM,
                         bg=self.BG3, fg=self.DIM).pack(side="left")
                tk.Scale(f2, from_=0, to=self._app.total,
                         orient="horizontal",
                         variable=self._app._seek_var,
                         command=lambda v: self._app._seek(int(float(v))),
                         bg=self.BG3, fg=self.ACC2,
                         highlightthickness=0, troughcolor=self.BG2,
                         length=400, showvalue=True,
                         font=self.FONT_SM).pack(side="left", fill="x", expand=True)

        # ── правая часть: статистика + графики ────────────────────
        right = tk.Frame(main, bg=self.BG, width=320)
        right.pack(side="left", fill="y")
        right.pack_propagate(False)
        left_pane = tk.Frame(right, bg=self.BG)
        left_pane.pack(fill="both", expand=True)

        self._build_stats(left_pane)
        self._build_pipeline(left_pane)
        self._build_alarms(left_pane)
        self._build_speed_chart(left_pane)
        self._build_re_chart(left_pane)
        self._build_radius_hist(left_pane)
        self._build_footer()   # ← status bar внизу окна

    # ── команды из GUI кнопок → worker thread ─────────────────────
    def _send_cmd(self, cmd):
        if self._app: self._app._cmd_q.put_nowait(cmd)

    def _toggle_pause(self):
        if not self._app: return
        if self._app._paused:
            self._send_cmd('resume')
            self._btn_pause.config(text="⏸ Пауза")
        else:
            self._send_cmd('pause')
            self._btn_pause.config(text="▶ Продолжить")

    # ── ROI через Canvas ───────────────────────────────────────────
    def _vc_press(self, e):
        if self._app: self._app._canvas_mouse_down(e.x, e.y)

    def _vc_drag(self, e):
        if self._app: self._app._canvas_mouse_move(e.x, e.y)

    def _vc_release(self, e):
        if self._app: self._app._canvas_mouse_up(e.x, e.y)

    def show_frame(self, frame_bgr):
        """Конвертирует BGR→RGB→PhotoImage и рисует на Canvas."""
        if not self._alive: return
        try:
            from PIL import Image, ImageTk
            cw = self._video_canvas.winfo_width()  or 640
            ch = self._video_canvas.winfo_height() or 480
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            scale = min(cw / w, ch / h, 1.0)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            img = Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(img)
            self._video_canvas.create_image(
                cw // 2, ch // 2, anchor="center", image=self._photo)
        except Exception:
            pass

    def _build_footer(self):
        """Footer: STATUS ACTIVE + runtime + source."""
        tk.Frame(self.root, bg=self.ACC, height=1).pack(fill="x")
        ftr = tk.Frame(self.root, bg="#060610", pady=4)
        ftr.pack(fill="x")
        # left: green dot + status
        fl = tk.Frame(ftr, bg="#060610")
        fl.pack(side="left", padx=12)
        tk.Label(fl, text="●", font=("Consolas",10), bg="#060610", fg=self.GRN).pack(side="left")
        tk.Label(fl, text=" SYSTEM STATUS: ACTIVE", font=("Consolas",9,"bold"),
                 bg="#060610", fg=self.GRN).pack(side="left")
        # right: runtime / sync / source
        fr = tk.Frame(ftr, bg="#060610")
        fr.pack(side="right", padx=12)
        self._ftr_runtime = tk.Label(fr, text="RUNTIME: 0.0s", font=("Consolas",9),
                                     bg="#060610", fg=self.DIM)
        self._ftr_runtime.pack(side="left", padx=8)
        src = os.path.basename(self.cfg.get("video_path","") or
                               f"Cam{self.cfg.get('camera_idx',0)}")
        tk.Label(fr, text=f"SYNC: OK  │  {src[:24]}  │  BTP",
                 font=("Consolas",9), bg="#060610", fg=self.DIM).pack(side="left")

    def _card(self, parent, title, title_color=None):
        """Карточка-контейнер."""
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="x", pady=(0,6))
        tk.Label(f, text=title, font=self.FONT_TITLE,
                 bg=self.BG2, fg=title_color or self.FG,
                 anchor="w").pack(fill="x", padx=10, pady=(6,2))
        return f

    def _stat_row(self, parent, label, key, color):
        row = tk.Frame(parent, bg=self.BG2)
        row.pack(fill="x", padx=10, pady=1)
        tk.Label(row, text=label, font=self.FONT_SM,
                 bg=self.BG2, fg=self.DIM, width=22, anchor="w").pack(side="left")
        v = tk.StringVar(value="—")
        self._stat_vars[key] = v
        tk.Label(row, textvariable=v, font=self.FONT_MONO,
                 bg=self.BG2, fg=color, anchor="e").pack(side="right", padx=6)

    # ── Панель статистики ────────────────────────────────────────────
    def _build_stats(self, parent):
        f = self._card(parent, "  📈 СТАТИСТИКА", self.ACC2)
        self._stat_vars = {}
        rows = [
            ("🫧 Всего пузырьков",    "total",    self.FG),
            ("⚡ Пузырьков / сек",    "rate",     self.GRN),
            ("📏 Средний радиус px",  "radius",   self.FG),
            ("🚀 Скорость px/s",      "speed_px", self.ACC2),
            ("🌡  Скорость мм/с",     "speed_mm", self.YEL),
            ("⚡ Скорость см/с",      "speed_cm", self.GRN),
            ("⚗  Число Reynolds",    "reynolds", self.ORG),
            ("🔸 Преоблад. форма",    "shape",    self.ACC),
            ("📐 px/мм (калибр.)",   "cal",      self.DIM),
            ("📹 FPS источника",      "fps",      self.DIM),
            ("🔲 Разрешение",         "res",      self.DIM),
            ("⏱  Длительность",      "dur",      self.DIM),
        ]
        for label, key, color in rows:
            self._stat_row(f, label, key, color)
        tk.Frame(f, bg=self.BG2, height=4).pack()

    # ── Конвейер / методы детекции ───────────────────────────────────
    def _build_pipeline(self, parent):
        f = self._card(parent, "  🔬 МЕТОДЫ ДЕТЕКЦИИ", self.ACC)
        self._pipe_vars = {}
        items = [
            ("Hough",          "hough"),
            ("Contour",        "contour"),
            ("Blob",           "blob"),
            ("Convexity Split","convex"),
            ("Watershed",      "watershed"),
        ]
        for label, key in items:
            row = tk.Frame(f, bg=self.BG2)
            row.pack(fill="x", padx=10, pady=1)
            tk.Label(row, text=label, font=self.FONT_SM,
                     bg=self.BG2, fg=self.DIM, width=18, anchor="w").pack(side="left")
            v = tk.StringVar(value="—")
            self._pipe_vars[key] = v
            tk.Label(row, textvariable=v, font=self.FONT_MONO,
                     bg=self.BG2, fg=self.ACC, anchor="e").pack(side="right", padx=6)
        tk.Frame(f, bg=self.BG2, height=4).pack()

    # ── Алармы ──────────────────────────────────────────────────────
    def _build_alarms(self, parent):
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="both", expand=True, pady=(0,0))
        tk.Label(f, text="  🚨 ТРЕВОГИ", font=self.FONT_TITLE,
                 bg=self.BG2, fg=self.ACC3, anchor="w").pack(fill="x", padx=10, pady=(6,2))
        self._alarm_canvas = tk.Canvas(f, bg=self.BG2, highlightthickness=0, height=100)
        self._alarm_canvas.pack(fill="both", expand=True, padx=8, pady=(0,6))

    # ── График скорости ──────────────────────────────────────────────
    def _build_speed_chart(self, parent):
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="both", expand=True, pady=(0,6))
        hdr = tk.Frame(f, bg=self.BG2); hdr.pack(fill="x")
        tk.Label(hdr, text="  📈 Скорость всплытия (px/s)",
                 font=self.FONT_TITLE, bg=self.BG2, fg=self.ACC2
                 ).pack(side="left", padx=10, pady=(6,2))
        self._speed_label = tk.Label(hdr, text="", font=self.FONT_MONO,
                                     bg=self.BG2, fg=self.GRN)
        self._speed_label.pack(side="right", padx=10)
        self._speed_canvas = tk.Canvas(f, bg=self.BG3, highlightthickness=0)
        self._speed_canvas.pack(fill="both", expand=True, padx=8, pady=(0,6))

    # ── График Reynolds ──────────────────────────────────────────────
    def _build_re_chart(self, parent):
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="both", expand=True, pady=(0,6))
        hdr = tk.Frame(f, bg=self.BG2); hdr.pack(fill="x")
        tk.Label(hdr, text="  ⚗  Число Рейнольдса (Re)",
                 font=self.FONT_TITLE, bg=self.BG2, fg=self.ORG
                 ).pack(side="left", padx=10, pady=(6,2))
        self._re_label = tk.Label(hdr, text="", font=self.FONT_MONO,
                                  bg=self.BG2, fg=self.ORG)
        self._re_label.pack(side="right", padx=10)
        self._re_canvas = tk.Canvas(f, bg=self.BG3, highlightthickness=0)
        self._re_canvas.pack(fill="both", expand=True, padx=8, pady=(0,6))

    # ── Гистограмма радиусов ─────────────────────────────────────────
    def _build_radius_hist(self, parent):
        f = tk.Frame(parent, bg=self.BG2, bd=0)
        f.pack(fill="both", expand=True)
        hdr = tk.Frame(f, bg=self.BG2); hdr.pack(fill="x")
        tk.Label(hdr, text="  📊 Распределение размеров (px)",
                 font=self.FONT_TITLE, bg=self.BG2, fg=self.YEL
                 ).pack(side="left", padx=10, pady=(6,2))
        self._radius_label = tk.Label(hdr, text="", font=self.FONT_MONO,
                                      bg=self.BG2, fg=self.YEL)
        self._radius_label.pack(side="right", padx=10)
        self._rad_canvas = tk.Canvas(f, bg=self.BG3, highlightthickness=0)
        self._rad_canvas.pack(fill="both", expand=True, padx=8, pady=(0,6))

    # ── Цикл обновления ──────────────────────────────────────────────
    def _schedule_update(self):
        if self._alive:
            try: self._update()
            except Exception: pass
            self.root.after(500, self._schedule_update)

    def _update(self):
        tr      = self.tracker
        log     = tr.detailed_log
        elapsed = time.time() - self._t0
        px_per_mm = self.cfg.get("px_per_mm", 10.0)

        total  = tr.total
        avg_r  = tr.avg_radius()
        rate   = tr.rate(elapsed) if elapsed > 0 else 0.0

        # скорость из velocities
        speeds_px = [abs(vy) * self.fps_actual
                     for _, (vx, vy) in tr.velocities.items() if vy != 0]
        avg_sp_px = float(np.mean(speeds_px)) if speeds_px else 0.0
        avg_sp_mm = avg_sp_px / px_per_mm if px_per_mm > 0 else 0.0

        # Reynolds из последних записей лога
        recent_re = [r.get("reynolds", 0) for r in log[-20:]] if log else []
        avg_re    = float(np.mean(recent_re)) if recent_re else 0.0

        # Преобладающая форма
        shape = "—"
        if log:
            recent_shapes = [r.get("shape", "circle") for r in log[-20:]]
            if recent_shapes:
                from collections import Counter
                shape = Counter(recent_shapes).most_common(1)[0][0]

        # история
        self._speed_hist.append(avg_sp_px)
        self._radius_hist.append(avg_r)
        self._rate_hist.append(rate)
        self._re_hist.append(avg_re)

        # ── KPI бейджи ───────────────────────────────────────────────
        self._kpi["total"]    = self._hdr_vars.get("schitano", self._kpi.get("total"))  # compat
        # ── Header stat vars ─────────────────────────────────────────
        if hasattr(self, '_hdr_vars'):
            hv = self._hdr_vars
            m2, s2 = divmod(int(elapsed), 60)
            hv["schitano" ].set(str(total))
            hv["on_screen"].set(str(tr.active_count))
            hv["radius"   ].set(f"{avg_r:.1f} px")
            hv["rate"     ].set(f"{rate:.2f}")
            hv["speed_mm" ].set(f"{avg_sp_mm:.2f}")
            hv["tracks"   ].set(str(tr.next_id - 1))
            hv["dur"      ].set(f"{m2:02d}:{s2:02d}")
        # ── Footer runtime ───────────────────────────────────────────
        if hasattr(self, '_ftr_runtime'):
            self._ftr_runtime.config(text=f"RUNTIME: {elapsed:.1f}s")

        # ── Статистика ───────────────────────────────────────────────
        m, s = divmod(int(elapsed), 60)
        sv = self._stat_vars
        sv["total"   ].set(str(total))
        sv["rate"    ].set(f"{rate:.2f}")
        sv["radius"  ].set(f"{avg_r:.1f} px")
        sv["speed_px"].set(f"{avg_sp_px:.1f}")
        sv["speed_mm"].set(f"{avg_sp_mm:.3f}")
        sv["speed_cm"].set(f"{(avg_sp_mm / 10.0):.4f}")
        sv["reynolds"].set(f"{avg_re:.0f}")
        sv["shape"   ].set(str(shape))
        sv["cal"     ].set(f"{px_per_mm:.2f}")
        sv["fps"     ].set(f"{self.fps_actual:.1f}")
        sv["res"     ].set(f"{self.res_w}×{self.res_h}")
        sv["dur"     ].set(f"{m:02d}:{s:02d}")
        self._lbl_clock.config(text=datetime.now().strftime("%H:%M:%S"))


        # ── Конвейер детекции (из BubbleTrackerApp._det_counts) ──────
        det = getattr(tr, "_det_counts_ext", {})
        self._pipe_vars["hough"    ].set(str(det.get("hough",     "—")))
        self._pipe_vars["contour"  ].set(str(det.get("contour",   "—")))
        self._pipe_vars["blob"     ].set(str(det.get("blob",      "—")))
        self._pipe_vars["convex"   ].set("Вкл." if self.cfg.get("use_convexity_split", True) else "Выкл.")
        self._pipe_vars["watershed"].set("Вкл." if self.cfg.get("use_watershed", True) else "Выкл.")

        # ── Графики ──────────────────────────────────────────────────
        self._draw_speed_chart(avg_sp_px, avg_sp_mm)
        self._draw_re_chart(avg_re)
        self._draw_radius_hist(log)
        self._draw_alarms(avg_sp_px, avg_r, rate, log, avg_re)

    # ── Рисуем линейный график (общий) ──────────────────────────────
    def _draw_line_chart(self, canvas, data, color, max_v=None,
                         unit="", ref_line=None, ref_label=""):
        c = canvas; c.delete("all")
        w = c.winfo_width(); h = c.winfo_height()
        if w < 20 or h < 20: return
        pad_l, pad_r, pad_t, pad_b = 44, 12, 8, 22
        data = list(data)
        mv = max(max(data) * 1.15, 1.0) if max_v is None else max_v
        # сетка
        for i in range(5):
            gy  = pad_t + (h - pad_t - pad_b) * i / 4
            val = mv * (1 - i / 4)
            c.create_line(pad_l, gy, w - pad_r, gy, fill=self.DIM, dash=(3,6))
            c.create_text(pad_l - 4, gy, text=f"{val:.0f}",
                          font=self.FONT_SM, fill=self.DIM, anchor="e")
        # ref line
        if ref_line and 0 < ref_line < mv:
            ry = pad_t + (h - pad_t - pad_b) * (1 - ref_line / mv)
            c.create_line(pad_l, ry, w - pad_r, ry,
                          fill=self.YEL, dash=(6,4), width=1)
            c.create_text(w - pad_r - 2, ry - 6, text=ref_label,
                          font=self.FONT_SM, fill=self.YEL, anchor="e")
        # кривая
        n  = len(data)
        xs = [pad_l + (w - pad_l - pad_r) * i / max(n-1, 1) for i in range(n)]
        ys = [pad_t + (h - pad_t - pad_b) * (1 - v / mv) for v in data]
        pts = []
        for xi, yi in zip(xs, ys): pts += [xi, yi]
        if len(pts) >= 4:
            c.create_line(*pts, fill=color, width=2, smooth=True)
        # текущая точка
        if pts:
            lx, ly = pts[-2], pts[-1]
            c.create_oval(lx-4, ly-4, lx+4, ly+4, fill=color, outline=self.BG)
        # оси
        c.create_line(pad_l, pad_t, pad_l, h-pad_b, fill=self.DIM)
        c.create_line(pad_l, h-pad_b, w-pad_r, h-pad_b, fill=self.DIM)

    def _draw_speed_chart(self, cur_px, cur_mm):
        # Линия Стокса
        avg_r_m = self.tracker.avg_radius() * self.tracker.px2m
        rho, mu, g = 1000.0, 0.001, 9.81
        v_st = (2*avg_r_m**2*(rho-1.2)*g)/(9*mu) if avg_r_m > 0 else 0
        v_st_px = v_st / self.tracker.px2m / self.fps_actual if self.tracker.px2m > 0 else 0
        self._draw_line_chart(self._speed_canvas, self._speed_hist,
                              self.ACC2, ref_line=v_st_px, ref_label="Stokes")
        self._speed_label.config(
            text=f"{cur_px:.1f} px/s  |  {cur_mm:.3f} мм/с")

    def _draw_re_chart(self, cur_re):
        alarm_re = self.cfg.get("alarm_re", 700)
        self._draw_line_chart(self._re_canvas, self._re_hist,
                              self.ORG, ref_line=alarm_re,
                              ref_label=f"Re={alarm_re}")
        color = self.RED if cur_re > alarm_re else self.GRN
        self._re_label.config(text=f"Re = {cur_re:.0f}", fg=color)

    def _draw_radius_hist(self, log):
        c = self._rad_canvas; c.delete("all")
        w = c.winfo_width(); h = c.winfo_height()
        if w < 20 or h < 20: return
        radii = [row["r_px"] for row in log if "r_px" in row] if log else []
        if not radii:
            c.create_text(w//2, h//2, text="Нет данных",
                          font=self.FONT_SM, fill=self.DIM); return
        mn = self.cfg.get("min_radius", 3)
        mx = self.cfg.get("max_radius", 25)
        n_bins = min(14, max(4, mx - mn))
        bins   = np.linspace(mn, mx, n_bins + 1)
        counts, _ = np.histogram(radii, bins=bins)
        mc = max(counts.max(), 1)
        pad_l, pad_r, pad_t, pad_b = 10, 10, 8, 22
        bw_raw = (w - pad_l - pad_r) / n_bins
        bw     = max(2, bw_raw - 3)
        for i, cnt in enumerate(counts):
            bh = (h - pad_t - pad_b) * cnt / mc
            x0 = pad_l + i * bw_raw
            x1 = x0 + bw
            y0 = h - pad_b - bh; y1 = h - pad_b
            ratio = cnt / mc
            r_c = int(0x7C + (0xFF - 0x7C) * ratio)
            b_c = int(0xFF * (1 - ratio * 0.4))
            c.create_rectangle(x0, y0, x1, y1,
                               fill=f"#{r_c:02X}4D{b_c:02X}", outline="")
            if i % max(1, n_bins // 6) == 0:
                c.create_text(x0 + bw/2, h - pad_b + 10,
                              text=f"{bins[i]:.0f}",
                              font=self.FONT_SM, fill=self.DIM)
        # медиана
        med_r = float(np.median(radii))
        med_x = pad_l + (med_r - mn) / max(mx - mn, 1) * (w - pad_l - pad_r)
        c.create_line(med_x, pad_t, med_x, h - pad_b,
                      fill=self.YEL, dash=(4,4), width=2)
        avg_r = float(np.mean(radii))
        self._radius_label.config(
            text=f"avg={avg_r:.1f}  med={med_r:.1f}  n={len(radii)}")

    def _draw_alarms(self, avg_sp_px, avg_r, rate, log, avg_re):
        c = self._alarm_canvas; c.delete("all")
        w = c.winfo_width()
        if w < 20: return
        alarms = []
        # Скорость упала
        sp = list(self._speed_hist)
        if len(sp) >= 20:
            if (np.mean(sp[:-5]) or 0) > 0 and np.mean(sp[-5:]) < np.mean(sp[:-5]) * 0.5:
                alarms.append(("🔴", "Скорость упала > 50%!", self.ACC3))
        # Reynolds
        alarm_re = self.cfg.get("alarm_re", 700)
        if avg_re > alarm_re:
            alarms.append(("🟠", f"Re={avg_re:.0f} > {alarm_re} (турб.)", self.ORG))
        # Поток
        if rate > 30:
            alarms.append(("🟡", f"Высокий поток: {rate:.1f}/с", self.YEL))
        # Малый радиус
        if 0 < avg_r < self.cfg.get("min_radius", 3) + 1:
            alarms.append(("🟠", f"Мелкие пузырьки: r={avg_r:.1f}px", self.YEL))
        if not alarms:
            alarms.append(("🟢", "Все параметры в норме", self.GRN))
        y = 14
        for icon, msg, col in alarms:
            c.create_text(14, y, text=icon, font=self.FONT_TITLE,
                          fill=col, anchor="w")
            c.create_text(40, y, text=msg, font=self.FONT_MONO,
                          fill=col, anchor="w")
            y += 26

    def close(self):
        self._alive = False
        try: self.root.destroy()
        except: pass

    def _on_close(self):
        self._alive = False


# ═══════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА

# ═══════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА

# ═══════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА

# ═══════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════
if __name__=="__main__":
    # Шаг 1: SettingsDialog — свой Tk(), файловый диалог работает нормально
    dlg = SettingsDialog()
    cfg = dlg.result
    if cfg is None:
        print("Отмена.")
    else:
        # ─── Шаг 2: Запускаем FastAPI в фоне ────────────────────────────────
        # data_dir указывает на bubble_results/ рядом со скриптом
        _data_dir = str(Path(__file__).parent / "bubble_results")
        start_api_server(data_dir=_data_dir, port=8000)

        # ─── Шаг 3: Запускаем React UI и открываем браузер ──────────────────
        _ui_dir = Path(__file__).parent / "bubble-dashboard-ui"
        if _ui_dir.exists():
            print("[FRONTEND] Запуск npm run dev в фоне...")
            import subprocess
            subprocess.Popen("npm run dev", cwd=str(_ui_dir), shell=True, 
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.0)  # Ждём пару секунд, чтобы Vite поднялся

        _ui_url = "http://localhost:5173"
        _open_browser(_ui_url)
        print(f"[DASHBOARD] {_ui_url}")
        print(f"[VIDEO STREAM] http://localhost:8000/api/stream")

        # ─── Шаг 4: Создаём Tk root и запускаем трекер ───────────────────────
        _app_root = tk.Tk()
        _app_root.withdraw()
        try:
            _app = BubbleTrackerApp(cfg, _app_root)
        except (FileNotFoundError, RuntimeError) as e:
            messagebox.showerror("Ошибка запуска", str(e))
            print(f"[ERROR] {e}")
            _app_root.destroy()
        except Exception as e:
            print(f"[CRITICAL] {e}")
            _app_root.destroy(); raise
        else:
            # run() регистрирует _tick() через after(), mainloop обрабатывает всё
            _app.run(app_root=_app_root)
            _app_root.mainloop()   # ← главный поток: события tkinter + OpenCV тики