"""网页面板测试：鉴权、配置热更新、目标增删、素材管理、写回 config.yaml。

用 FastAPI TestClient，不联网、不需要协议号。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target  # noqa: E402
from tgrelay.config_store import ConfigStore  # noqa: E402
from tgrelay.control import RuntimeControl  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402
from tgrelay.webapp import LogBuffer, create_app  # noqa: E402

TOKEN = "test-token-123"

CONFIG_TEMPLATE = """\
# 手写注释：这一行必须被保留下来
sources:
  - "@src"

# 目标群的注释
targets:
  - id: -1002001
    label: 群A
    interval: [30, 35]

rate:
  per_target_interval: [5, 10]
  cross_target_delay: [5, 15]
  global_per_minute: 10    # 手写尾注
  daily_cap: 100

behavior:
  queue_size: 10

repost:
  enabled: true
  ids: [6]
  interval: 300
  daily_limit: 200
"""


class PermClient(FakeClient):
    """可发言、无慢速的假客户端（供 add_target 的校验用）。

    `default_banned_rights` 的 flag 是**反的**：True = 禁止。
    这里全部 False 表示"普通开放群"，也就是这个号能往里发东西。
    （早期全写成 True，等于"文字/媒体/图片全禁"，判定自然变成不能发。）
    """

    def __init__(self, slowmode: int = 0) -> None:
        super().__init__()
        self._slowmode = slowmode
        self.fetched: list[int] = []

    async def get_entity(self, peer: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=-1003003,
            title="新群",
            default_banned_rights=SimpleNamespace(
                send_messages=False,
                send_plain=False,
                send_media=False,
                send_photos=False,
            ),
            slowmode_seconds=self._slowmode or None,
        )

    async def get_messages(self, peer: object, ids: object = None, **kwargs: object) -> object:
        """素材热更新会调这个：按请求的 id 返回假消息。"""
        if ids is None:
            return fake_message(msg_id=6, chat_id=-1001, text="素材6")
        pairs = ids if isinstance(ids, (list, tuple)) else [ids]
        out = []
        for item in pairs:
            msg_id = int(item)
            self.fetched.append(msg_id)
            out.append(fake_message(msg_id=msg_id, chat_id=-1001, text=f"素材{msg_id}"))
        return out


@pytest.fixture()
def wired(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")

    from tgrelay.config import load_config

    config = load_config(config_path)
    store = Store(tmp_path / "relay.db")
    client = PermClient()
    sender = Sender(client, config, store)

    class Engine(RelayEngine):
        """把 add_target 的异步启动简化：测试里不需要真的跑 worker 循环。"""

    engine = RelayEngine(config, store, sender)
    engine.attach(client)

    reposter = Reposter(config, store, sender, client=client)
    reposter.messages = [fake_message(msg_id=6, chat_id=-1001, text="素材6")]
    reposter._order = [0]
    reposter.source_peer = "src"
    reposter.source_id = -1001

    control = RuntimeControl(
        config,
        store,
        sender,
        engine,
        config_store=ConfigStore(config_path),
        reposter=reposter,
        client=client,
        log_path=tmp_path / "relay.log",
        config_path=config_path,
    )
    app = create_app(control, token=TOKEN, log_buffer=LogBuffer())
    with TestClient(app) as http:
        yield http, control, config_path, store, engine
    store.close()


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------


def test_requires_token(wired) -> None:
    http, *_ = wired
    assert http.get("/api/stats").status_code == 401
    assert http.get("/api/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert http.get("/api/stats", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_token_in_query_sets_cookie(wired) -> None:
    http, *_ = wired
    response = http.get(f"/?token={TOKEN}")
    assert response.status_code == 200
    assert "tg-relay" in response.text
    # 之后不带 header 也能访问（Cookie 生效）
    assert http.get("/api/stats").status_code == 200


def test_cookie_is_secure_behind_https_proxy(wired) -> None:
    """走 HTTPS 反代时 Cookie 必须带 Secure，否则明文端口也能带走它。"""
    http, *_ = wired
    response = http.get(
        f"/?token={TOKEN}", headers={"X-Forwarded-Proto": "https"}
    )
    cookie = response.headers.get("set-cookie", "")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


def test_cookie_not_secure_on_plain_http(wired) -> None:
    """本地直接 http 访问时不加 Secure，否则浏览器会拒绝保存 Cookie。"""
    http, *_ = wired
    response = http.get(f"/?token={TOKEN}")
    cookie = response.headers.get("set-cookie", "")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


def test_wrong_token_does_not_set_cookie(wired) -> None:
    http, *_ = wired
    response = http.get("/?token=wrong")
    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_health_is_public(wired) -> None:
    http, *_ = wired
    assert http.get("/health").json()["ok"] is True


def test_websocket_rejects_bad_token(wired) -> None:
    http, *_ = wired
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect("/api/ws?token=nope"):
            pass


def test_websocket_pushes_snapshot(wired) -> None:
    http, *_ = wired
    with http.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
        payload = ws.receive_json()
    assert payload["type"] == "snapshot"
    assert "targets" in payload["data"]
    assert isinstance(payload["logs"], list)


# --------------------------------------------------------------------------
# 状态与日志
# --------------------------------------------------------------------------


def test_stats_shape(wired) -> None:
    http, *_ = wired
    data = http.get("/api/stats", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert set(data) >= {"engine", "sender", "targets", "store", "config", "repost"}
    assert data["config"]["sources"] == ["@src"]
    assert data["repost"]["enabled"] is True
    assert data["repost"]["materials"][0]["msg_id"] == 6


def test_logs_endpoint(wired) -> None:
    """优先读日志文件（比内存缓冲更完整）。"""
    http, control, *_ = wired
    control.log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    data = http.get("/api/logs?limit=2", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert data["lines"] == ["line2", "line3"]


def test_logs_falls_back_to_buffer(wired) -> None:
    """日志文件不存在时，回落到内存环形缓冲。

    注意：这里不能用 `app.state.log_buffer = ...` 替换——端点闭包引用的是
    建 app 时传入的那个对象，替换 state 不会生效。所以用一个新的 app 来测。
    """
    import logging

    from tgrelay.control import RuntimeControl
    from tgrelay.webapp import LogBuffer, create_app

    http, control, *_ = wired
    assert not control.log_path.exists()  # 确认走的是回落分支

    buffer = LogBuffer()
    logging.getLogger().addHandler(buffer)
    try:
        logging.getLogger("tgrelay.test").warning("来自内存缓冲的一行")
        app = create_app(control, token=TOKEN, log_buffer=buffer)
        with TestClient(app) as client:
            data = client.get("/api/logs", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert any("来自内存缓冲的一行" in line for line in data["lines"])
    finally:
        logging.getLogger().removeHandler(buffer)


def test_log_missing_file_is_empty(wired) -> None:
    http, *_ = wired
    data = http.get("/api/logs", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert data["lines"] == []


# --------------------------------------------------------------------------
# 目标群增删改
# --------------------------------------------------------------------------


def test_add_target_writes_config_and_creates_worker(wired) -> None:
    http, control, config_path, store, engine = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = http.post("/api/targets", json={"peer": "-1003003", "label": "新群"}, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()["target"]
    assert body["label"] == "新群"

    # worker 立刻存在（热生效，不用重启）
    assert engine.worker_of(-1003003) is not None
    # 写回了 config.yaml，且手写注释还在
    text = config_path.read_text(encoding="utf-8")
    assert "手写注释：这一行必须被保留下来" in text
    assert "-1003003" in text


def test_add_target_rejects_duplicate(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert http.post("/api/targets", json={"peer": "-1002001"}, headers=auth).status_code == 400


def test_add_target_aligned_to_slowmode(wired) -> None:
    """群有慢速时，新目标的 interval 应自动对齐过去。"""
    http, control, *_ = wired
    control.sender.client._slowmode = 30
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = http.post(
        "/api/targets", json={"peer": "-1003003", "label": "慢速群"}, headers=auth
    ).json()["target"]
    assert body["slowmode"] == 30
    assert body["interval"] == [30.0, 35.0]


def test_patch_and_pause_target(wired) -> None:
    http, _, _, _, engine = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    key = "-1002001"
    assert http.patch(f"/api/targets/{key}", json={"interval": [40, 45]}, headers=auth).status_code == 200
    worker = engine.worker_of(key)
    assert worker.target.interval == (40.0, 45.0)

    assert http.post(f"/api/targets/{key}/pause", headers=auth).json()["paused"] is True
    assert worker.paused is True
    assert http.post(f"/api/targets/{key}/resume", headers=auth).json()["paused"] is False
    assert worker.paused is False


def test_patch_rejects_bad_interval(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = http.patch("/api/targets/-1002001", json={"interval": [50, 10]}, headers=auth)
    assert response.status_code == 400


def test_delete_target(wired) -> None:
    http, _, _, _, engine = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    key = "-1002001"
    assert key in engine.workers
    assert http.delete(f"/api/targets/{key}", headers=auth).status_code == 200
    assert engine.worker_of(key) is None


def test_delete_unknown_target_is_400(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert http.delete("/api/targets/nope", headers=auth).status_code == 400


def test_check_target_endpoint(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    data = http.post("/api/targets/-1002001/check", headers=auth).json()
    assert data["can_post"] is True


# --------------------------------------------------------------------------
# 素材与重发
# --------------------------------------------------------------------------


def test_set_materials_updates_config_and_config_file(wired) -> None:
    http, control, config_path, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = http.put("/api/materials", json={"ids": [9, 7, 7]}, headers=auth)
    assert response.status_code == 200
    assert response.json()["ids"] == [7, 9]  # 排序去重
    assert control.config.repost.ids == (7, 9)
    text = config_path.read_text(encoding="utf-8")
    assert "ids: [7, 9]" in text


def test_set_materials_rejects_empty(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert http.put("/api/materials", json={"ids": []}, headers=auth).status_code == 400


def test_repost_interval_below_slow_mode_is_rejected(wired) -> None:
    """网页也不允许把间隔设到 30s 以下。"""
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = http.patch("/api/repost", json={"interval": 20}, headers=auth)
    assert response.status_code == 400
    assert "31" in response.json()["detail"]


def test_repost_options_roundtrip(wired) -> None:
    http, control, config_path, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    data = http.patch(
        "/api/repost", json={"interval": 600, "daily_limit": 50, "shuffle": True}, headers=auth
    ).json()
    assert data == {"enabled": True, "interval": 600.0, "daily_limit": 50, "shuffle": True}
    assert control.config.repost.interval == 600.0
    assert "interval: 600" in config_path.read_text(encoding="utf-8")


def test_run_repost_once(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    data = http.post("/api/repost/run", headers=auth).json()
    assert data["sent"] >= 0
    assert "stats" in data


# --------------------------------------------------------------------------
# 停止 / 启动重发必须是一对可逆操作
#
# 2026-09-12 事故：账号被 Telegram 限制后需要"停掉重发等申诉"，
# 但当时的面板只能停不能开，停了就只能重启进程。这几个测试锁死可逆性。
# --------------------------------------------------------------------------


def test_stop_repost_disables_and_persists(wired) -> None:
    http, control, config_path, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    data = http.post("/api/repost/stop", headers=auth).json()

    assert data["stopping"] is True
    assert data["enabled"] is False
    assert control.config.repost.enabled is False
    # 必须落盘：否则服务器一重启，自己又开始往被限制的号上发
    assert "enabled: false" in config_path.read_text(encoding="utf-8")


def test_start_repost_is_the_inverse_of_stop(wired) -> None:
    http, control, config_path, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}

    http.post("/api/repost/stop", headers=auth)
    assert control.reposter.running is False

    data = http.post("/api/repost/start", headers=auth).json()

    assert data["started"] is True
    assert data["enabled"] is True
    assert control.config.repost.enabled is True
    assert control.reposter.running is True, "停止之后必须还能启动回来"
    assert "enabled: true" in config_path.read_text(encoding="utf-8")

    control.reposter._stop.set()


def test_start_repost_clears_breaker_and_blocks(wired) -> None:
    """人主动点「启动重发」= 已经处理过账号问题，顺手松开刹车。"""
    http, control, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}

    control.sender.breaker.trip("测试熔断")
    control.sender.write_forbidden.note("-1002001")

    http.post("/api/repost/start", headers=auth)

    assert control.sender.breaker.tripped is False
    assert control.sender.write_forbidden.blocked_targets() == {}
    control.reposter._stop.set()


def test_stats_expose_account_section(wired) -> None:
    """账号状态要能在面板上看到：熔断原因、被停发的目标、最近告警。"""
    http, control, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    control.sender.breaker.trip("多个目标报禁止发言")
    control.sender.write_forbidden.note("-1002001")

    data = http.get("/api/stats", headers=auth).json()

    assert data["account"]["breaker"]
    assert "-1002001" in data["account"]["write_forbidden"]


def test_account_check_reports_limited(wired) -> None:
    """账号自检：@SpamBot 说被限制时如实回报，并且说清该干什么。"""
    http, control, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}

    async def fake_status() -> tuple[bool | None, str]:
        return True, "While the account is limited…"

    control.sender.account_status = fake_status  # type: ignore[assignment]
    data = http.post("/api/account/check", headers=auth).json()

    assert data["limited"] is True
    assert "申诉" in data["message"]


def test_account_check_releases_brakes_when_clear(wired) -> None:
    """确认没限制了：把熔断和停发状态一起松开，不用重启进程。"""
    http, control, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    control.sender.breaker.trip("测试熔断")
    control.sender.write_forbidden.note("-1002001")

    async def fake_status() -> tuple[bool | None, str]:
        return False, "Good news, no limits are currently applied to your account."

    control.sender.account_status = fake_status  # type: ignore[assignment]
    data = http.post("/api/account/check", headers=auth).json()

    assert data["limited"] is False
    assert control.sender.breaker.tripped is False
    assert control.sender.write_forbidden.blocked_targets() == {}


# --------------------------------------------------------------------------
# 限速
# --------------------------------------------------------------------------


def test_patch_rate_updates_pacer_and_file(wired) -> None:
    http, control, config_path, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    data = http.patch("/api/rate", json={"global_per_minute": 25, "daily_cap": 300}, headers=auth).json()
    assert data == {"global_per_minute": 25, "daily_cap": 300}
    assert control.sender.pacer.global_per_minute == 25
    assert control.sender.pacer.daily_cap == 300

    text = config_path.read_text(encoding="utf-8")
    assert "global_per_minute: 25" in text
    assert "daily_cap: 300" in text
    # 手写尾注要保住
    assert "# 手写尾注" in text


def test_patch_rate_rejects_zero(wired) -> None:
    http, *_ = wired
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert http.patch("/api/rate", json={"global_per_minute": 0}, headers=auth).status_code == 400
