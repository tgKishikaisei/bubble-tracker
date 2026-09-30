"""Тесты доступа к API трекера. Запуск из папки bubble-api: python -m pytest tests"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DATA = Path(tempfile.mkdtemp())
(DATA / "details").mkdir()
(DATA / "details" / "session_2026-04-05_12-30-00.csv").write_text("Id;Radius\n1;2,5\n", encoding="utf-8")
(DATA.parent / "secret.csv").write_text("password;x\n1;2\n", encoding="utf-8")
os.environ["DATA_DIR"] = str(DATA)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
DASHBOARD = {"X-Bubble-Client": "dashboard", "Origin": "http://localhost:5173"}


def setup_function():
    main.tracker_commands.update(pause=False, stop=False, reset=False, line_y_pct=None)


def test_command_from_dashboard_works():
    assert client.post("/api/tracker/command", params={"cmd": "pause"}, headers=DASHBOARD).status_code == 200
    assert main.tracker_commands["pause"] is True


@pytest.mark.parametrize("headers", [
    {},                                                                  # простой POST из чужой вкладки
    {"X-Bubble-Client": "dashboard", "Origin": "https://evil.example"},  # чужой сайт
])
def test_foreign_commands_are_rejected(headers):
    assert client.post("/api/tracker/command", params={"cmd": "stop"}, headers=headers).status_code == 403
    assert client.post("/api/tracker/line_y", params={"pct": 50}, headers=headers).status_code == 403
    assert main.tracker_commands["stop"] is False


def test_cors_preflight_only_for_dashboard():
    ok = client.options("/api/tracker/command", headers={"Origin": "http://localhost:5173",
                                                         "Access-Control-Request-Method": "POST",
                                                         "Access-Control-Request-Headers": "X-Bubble-Client"})
    bad = client.options("/api/tracker/command", headers={"Origin": "https://evil.example",
                                                          "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": "X-Bubble-Client"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


def test_details_by_session_id():
    rows = client.get("/api/details/2026-04-05_12-30-00").json()
    assert rows == [{"Id": 1, "Radius": 2.5}]


@pytest.mark.parametrize("session_id", ["x%5C..%5C..%5C..%5Csecret", "..%5C..%5Csecret", "*", "..", "a%5Cb"])
def test_session_id_cannot_escape_data_dir(session_id):
    response = client.get(f"/api/details/{session_id}")
    assert response.status_code in (404, 422) and "password" not in response.text


def test_websocket_rejects_foreign_origin():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws/stats", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
