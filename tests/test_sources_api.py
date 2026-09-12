"""源频道 API 测试：列表、追加、换源、移除。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from test_sources import build  # noqa: E402
from tgrelay.webapp import create_app  # noqa: E402

TOKEN = "t"


@pytest.fixture()
def client(tmp_path: Path):
    control, engine, store, fake, reposter, listener, path = build(tmp_path)
    app = create_app(control, token=TOKEN)
    with TestClient(app) as http:
        yield http, control, path, store
    store.close()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_list_sources(client) -> None:
    http, control, _, _ = client
    data = http.get("/api/sources", headers=AUTH).json()
    assert data["sources"] == [{"peer": "@old_channel", "主源": True}]


def test_requires_token(client) -> None:
    http, *_ = client
    assert http.get("/api/sources").status_code == 401


def test_add_source(client) -> None:
    http, control, path, _ = client
    response = http.post("/api/sources", json={"peer": "@extra", "verify": False}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["added"] == "@extra"
    assert control.config.sources == ("@old_channel", "@extra")


def test_add_duplicate_is_400(client) -> None:
    http, *_ = client
    response = http.post(
        "/api/sources", json={"peer": "@old_channel", "verify": False}, headers=AUTH
    )
    assert response.status_code == 400


def test_switch_source(client) -> None:
    http, control, path, _ = client
    response = http.post(
        "/api/sources/switch", json={"peer": "@new_channel", "verify": False}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["old"] == "@old_channel"
    assert body["source"] == "@new_channel"
    assert control.config.source == "@new_channel"
    assert "@new_channel" in path.read_text(encoding="utf-8")


def test_switch_source_empty_is_400(client) -> None:
    http, *_ = client
    response = http.post("/api/sources/switch", json={"peer": "  ", "verify": False}, headers=AUTH)
    assert response.status_code == 400


def test_remove_source(client) -> None:
    http, control, _, _ = client
    http.post("/api/sources", json={"peer": "@extra", "verify": False}, headers=AUTH)
    response = http.delete("/api/sources/@extra", headers=AUTH)
    assert response.status_code == 200
    assert control.config.sources == ("@old_channel",)


def test_cannot_remove_last_source(client) -> None:
    http, *_ = client
    response = http.delete("/api/sources/@old_channel", headers=AUTH)
    assert response.status_code == 400
    assert "至少要保留" in response.json()["detail"]
