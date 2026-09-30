"""
bubble-api/main.py  v4.0
========================
FastAPI бэкенд для Bubble Tracker PRO.

v4.0:
  • WebSocket /api/ws/stats  — push статистики раз в секунду (вместо polling)
  • GET /api/stream           — MJPEG видеопоток (Producer-Consumer, last-frame)
  • GET /api/live_stats       — REST fallback для live JSON метрик
  • POST /api/tracker/command — управляющие команды
"""

import os
import re
import json
import glob
import time
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Generator, Set

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
#  ГЛОБАЛЬНЫЙ FRAME PUBLISHER  (Producer-Consumer, last-frame drop)
# ─────────────────────────────────────────────────────────────────────────────
class FramePublisher:
    """
    Thread-safe хранилище последнего JPEG-кадра.
    Producer (трекер) → push_frame() → Consumer (/api/stream) → iter_frames()
    Если трекер быстрее браузера — старые кадры дропаются (no queue build-up).
    """
    def __init__(self):
        self._frame: Optional[bytes] = None
        self._cond  = threading.Condition()
        self._frame_count = 0

    def push_frame(self, jpeg_bytes: bytes):
        with self._cond:
            self._frame = jpeg_bytes
            self._frame_count += 1
            self._cond.notify_all()

    def iter_frames(self) -> Generator[bytes, None, None]:
        last_count = -1
        while True:
            with self._cond:
                ok = self._cond.wait_for(
                    lambda: self._frame_count != last_count,
                    timeout=2.0
                )
                if not ok:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    continue
                frame = self._frame
                last_count = self._frame_count
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )

    @property
    def is_active(self) -> bool:
        return self._frame is not None


# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET MANAGER — рассылает статистику подписчикам
# ─────────────────────────────────────────────────────────────────────────────
class StatsWsManager:
    """Broadcast последней статистики всем подключённым WebSocket клиентам."""
    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, data: dict):
        """Рассылает всем живым клиентам; удаляет отвалившихся."""
        msg = json.dumps(data)
        dead: Set[WebSocket] = set()
        async with self._lock:
            clients = set(self._clients)
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._clients -= dead


# ─────────────────────────────────────────────────────────────────────────────
#  ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ─────────────────────────────────────────────────────────────────────────────
frame_publisher  = FramePublisher()
stats_ws_manager = StatsWsManager()
live_stats: dict = {}
tracker_commands: dict = {
    "pause": False,
    "reset": False,
    "stop":  False,
    "line_y_pct": None,
}


# ─────────────────────────────────────────────────────────────────────────────
#  FastAPI APP
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_stats_broadcast_loop())
    yield
    task.cancel()


app = FastAPI(title="Bubble Tracker API", version="4.1.0", lifespan=lifespan)
log = logging.getLogger("bubble-api")

# CORS только для адресов дашборда: иначе любой сайт в браузере оператора
# мог бы слать трекеру команды (stop/reset) и читать статистику.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "BUBBLE_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
).split(",") if o.strip()]
# Заголовок, который отправляет только наш дашборд. С ним запрос с чужого сайта требует
# CORS-preflight, а его браузер не пропустит — «тихий» POST из чужой вкладки невозможен.
CLIENT_HEADER = "X-Bubble-Client"
SESSION_ID_RE = re.compile(r"^[\w.-]{1,80}$")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[CLIENT_HEADER, "Content-Type"],
)


def require_dashboard(request: Request, x_bubble_client: Optional[str] = Header(default=None)):
    origin = request.headers.get("origin")
    if x_bubble_client != "dashboard" or (origin and origin not in ALLOWED_ORIGINS):
        raise HTTPException(status_code=403, detail="Команды принимаются только от дашборда")


def check_session_id(session_id: str) -> str:
    # session_id уходит в glob и путь к файлу: «..\\» вывел бы за пределы папки
    # результатов, а «*» подобрал бы чужие файлы
    if not SESSION_ID_RE.match(session_id) or ".." in session_id:
        raise HTTPException(status_code=422, detail="Некорректный идентификатор сессии")
    return glob.escape(session_id)

DATA_DIR      = Path(os.environ.get("DATA_DIR", "/app/data"))
SESSIONS_JSON = DATA_DIR / "sessions.json"
DETAILS_DIR   = DATA_DIR / "details"


# ─────────────────────────────────────────────────────────────────────────────
#  BACKGROUND TASK: push статистики через WS раз в секунду
# ─────────────────────────────────────────────────────────────────────────────
async def _stats_broadcast_loop():
    """Отдельная задача: рассылает live_stats всем WS-клиентам каждую секунду."""
    while True:
        await asyncio.sleep(1.0)
        if live_stats and stats_ws_manager._clients:
            await stats_ws_manager.broadcast(live_stats)


# ─────────────────────────────────────────────────────────────────────────────
#  ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
def health_check():
    return {
        "status": "ok",
        "app": "bubble-api",
        "version": "4.0.0",
        "stream_active": frame_publisher.is_active,
        "ws_clients": len(stats_ws_manager._clients),
    }


@app.get("/api/stream")
def video_stream():
    """MJPEG поток — браузер рендерит через <img src='/api/stream'>."""
    return StreamingResponse(
        frame_publisher.iter_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.websocket("/api/ws/stats")
async def ws_stats(ws: WebSocket):
    """
    WebSocket endpoint для push-статистики.
    Фронтенд подключается один раз — получает данные каждую секунду без polling.
    """
    # Проверка Origin: без неё любой сайт мог подключиться к WebSocket (CSWSH) и читать статистику
    origin = ws.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await stats_ws_manager.connect(ws)
    try:
        # сразу шлём текущее состояние
        if live_stats:
            await ws.send_text(json.dumps(live_stats))
        # держим соединение живым, принимаем ping от клиента
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # клиент молчит — ок, соединение живо
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await stats_ws_manager.disconnect(ws)


@app.get("/api/live_stats")
def get_live_stats():
    """REST fallback для live метрик (если WS недоступен)."""
    return live_stats if live_stats else {
        "active": False, "total": 0, "on_screen": 0,
        "rate": 0.0, "elapsed": 0.0, "avg_radius": 0.0,
    }


@app.post("/api/tracker/command", dependencies=[Depends(require_dashboard)])
def send_command(cmd: str = Query(..., description="pause|resume|reset|stop")):
    """Отправляет команду трекеру."""
    global tracker_commands
    if cmd == "pause":
        tracker_commands["pause"] = True
    elif cmd == "resume":
        tracker_commands["pause"] = False
    elif cmd == "reset":
        tracker_commands["reset"] = True
    elif cmd == "stop":
        tracker_commands["stop"] = True
    else:
        raise HTTPException(status_code=400, detail="Неизвестная команда")
    return {"ok": True, "cmd": cmd}


@app.post("/api/tracker/line_y", dependencies=[Depends(require_dashboard)])
def set_line_y(pct: int = Query(..., ge=1, le=99)):
    global tracker_commands
    tracker_commands["line_y_pct"] = pct
    return {"ok": True, "line_y_pct": pct}


# ─────────────────────────────────────────────────────────────────────────────
#  ИСТОРИЯ СЕССИЙ
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/sessions")
def get_sessions(min_bubbles: int = Query(default=0)):
    if not SESSIONS_JSON.exists():
        return []
    try:
        with open(SESSIONS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if min_bubbles > 0:
            data = [s for s in data if s.get("total_bubbles", 0) >= min_bubbles]
        return data
    except Exception:
        log.exception("Ошибка чтения sessions.json")
        raise HTTPException(status_code=500, detail="Ошибка чтения истории сессий")


@app.get("/api/sessions/stats")
def get_sessions_stats():
    if not SESSIONS_JSON.exists():
        return {}
    try:
        with open(SESSIONS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = [s for s in data if s.get("total_bubbles", 0) > 0]
        if not valid:
            return {"total_sessions": 0, "total_bubbles": 0}
        return {
            "total_sessions": len(valid),
            "total_bubbles": sum(s.get("total_bubbles", 0) for s in valid),
            "avg_rate_per_sec": round(sum(s.get("rate_per_sec", 0) for s in valid) / len(valid), 2),
            "avg_radius_px": round(sum(s.get("avg_radius_px", 0) for s in valid) / len(valid), 2),
        }
    except Exception:
        log.exception("Ошибка чтения sessions.json")
        raise HTTPException(status_code=500, detail="Ошибка чтения истории сессий")


@app.get("/api/details/{session_id}")
def get_session_details(session_id: str):
    session_id = check_session_id(session_id)
    filename = DETAILS_DIR / f"session_{session_id}.csv"
    if not filename.exists():
        files = glob.glob(str(DETAILS_DIR / f"*{session_id}*.csv"))
        files = [f for f in files if "per_frame" not in f]
        if not files:
            raise HTTPException(status_code=404, detail="Детализация не найдена")
        filename = Path(files[0])
    try:
        df = pd.read_csv(filename, sep=";", decimal=",", encoding="utf-8-sig")
        if len(df.columns) == 1:
            df = pd.read_csv(filename, sep=",", decimal=".", encoding="utf-8-sig")
        df = df.where(pd.notnull(df), None)
        df.rename(columns={
            "Speed_px_per_frame": "Speed_px_s",
            "Speed_mm_per_frame": "Speed_mm_s",
        }, inplace=True)
        return df.to_dict(orient="records")
    except Exception:
        log.exception("Ошибка чтения детализации")
        raise HTTPException(status_code=500, detail="Ошибка чтения детализации")


@app.get("/api/heatmap/{session_id}")
def get_heatmap(session_id: str):
    session_id = check_session_id(session_id)
    files = glob.glob(str(DATA_DIR / f"heatmap_{session_id}.png"))
    if not files:
        raise HTTPException(status_code=404, detail="Тепловая карта не найдена")
    return FileResponse(files[0], media_type="image/png")


if __name__ == "__main__":
    import uvicorn
    # По умолчанию только этот компьютер; в Docker задаётся BUBBLE_API_HOST=0.0.0.0
    uvicorn.run("main:app", host=os.environ.get("BUBBLE_API_HOST", "127.0.0.1"), port=8000, reload=False)
