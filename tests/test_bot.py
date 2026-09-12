"""操控 Bot 测试：白名单、命令分发、回复内容。不联网、不需要真 bot。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.bot import BotController, BotError  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target  # noqa: E402
from tgrelay.control import RuntimeControl  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402
from tgrelay.config_store import ConfigStore  # noqa: E402

CONFIG = """\
sources: ["@src"]
targets:
  - id: -1002001
    label: 群A
    interval: [30, 35]
rate:
  per_target_interval: [5, 10]
  cross_target_delay: [5, 15]
  global_per_minute: 10
  daily_cap: 100
behavior:
  queue_size: 10
repost:
  enabled: true
  ids: [6]
  interval: 300
  daily_limit: 200
"""


class FakeEvent:
    """够用的假事件：记录回复内容。"""

    def __init__(self, text: str, sender_id: int, replies: list[str]) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self._replies = replies

    async def reply(self, text: str, **kwargs: Any) -> None:
        self._replies.append(text)


@pytest.fixture()
def bot(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")

    from tgrelay.config import load_config

    config = load_config(config_path)
    store = Store(tmp_path / "relay.db")
    client = FakeClient()

    class PostableClient(FakeClient):
        async def get_entity(self, peer: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(
                id=-1002001,
                title="群A",
                default_banned_rights=SimpleNamespace(send_messages=True),
                slowmode_seconds=30,
            )

        async def get_messages(self, peer: Any, ids: Any = None, **kw: Any) -> Any:
            pairs = ids if isinstance(ids, (list, tuple)) else [ids]
            return [fake_message(msg_id=int(i), chat_id=-1001, text=f"素材{i}") for i in pairs]

    client = PostableClient()
    sender = Sender(client, config, store)
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
    controller = BotController(
        control,
        token="fake:token",
        admins=(111,),
        admin_file=tmp_path / "bot_admin.txt",
    )
    yield controller, control, tmp_path
    store.close()


async def send(controller: BotController, text: str, sender_id: int = 111) -> str:
    replies: list[str] = []
    await controller.handle(FakeEvent(text, sender_id, replies))
    assert len(replies) == 1
    return replies[0]


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------


async def test_stranger_is_ignored(bot) -> None:
    controller, *_ = bot
    replies: list[str] = []
    await controller.handle(FakeEvent("/status", 999, replies))
    assert replies == []  # 完全不理，不回消息（不暴露自己存在）


async def test_first_sender_becomes_admin_when_list_empty(bot, tmp_path: Path) -> None:
    controller, *_ = bot
    controller.allowed.clear()
    reply = await send(controller, "/start", sender_id=555)
    assert "管理员" in reply
    assert 555 in controller.allowed
    assert "555" in (tmp_path / "bot_admin.txt").read_text(encoding="utf-8")


async def test_admin_file_is_loaded(bot, tmp_path: Path) -> None:
    admin_file = tmp_path / "bot_admin.txt"
    admin_file.write_text("777\n888\n", encoding="utf-8")
    controller, control, _ = bot
    controller.admin_file = admin_file
    controller.allowed = {111}
    controller._load_admins()
    assert {777, 888} <= controller.allowed


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


async def test_non_command_gets_hint(bot) -> None:
    controller, *_ = bot
    assert "命令" in await send(controller, "你好")


async def test_unknown_command_shows_help(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/nosuchcmd")
    assert "没有这个命令" in reply
    assert "/status" in reply


async def test_help_and_start(bot) -> None:
    controller, *_ = bot
    for command in ("/help", "/start"):
        reply = await send(controller, command)
        assert "/status" in reply and "/targets" in reply


async def test_command_with_bot_username_suffix(bot) -> None:
    """/status@my_bot 这种写法也要能识别（Telegram 群里常见）。"""
    controller, *_ = bot
    reply = await send(controller, "/status@some_bot")
    assert "运行状态" in reply


async def test_status_lists_targets_with_index(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/status")
    assert "运行状态" in reply
    assert "群A" in reply
    assert "1." in reply
    assert controller._meta[1] == "群A"


async def test_where_shows_config(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/where")
    assert "@src" in reply
    assert "10" in reply  # 全局上限


async def test_materials_list(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/materials")
    assert "6" in reply


async def test_addmat_updates_config(bot, tmp_path: Path) -> None:
    controller, control, _ = bot
    reply = await send(controller, "/addmat 6 7 8")
    assert "素材已设为" in reply
    assert control.config.repost.ids == (6, 7, 8)
    assert "ids: [6, 7, 8]" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


async def test_addmat_rejects_empty(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/addmat abc")
    assert "❌" in reply


async def test_interval_below_slow_mode_is_rejected(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/interval 10")
    assert "❌" in reply
    assert "31" in reply


async def test_dailylimit(bot) -> None:
    controller, control, _ = bot
    reply = await send(controller, "/dailylimit 50")
    assert "50" in reply
    assert control.config.repost.daily_limit == 50


async def test_off_and_on_by_index(bot) -> None:
    controller, control, _ = bot
    reply = await send(controller, "/status")  # 先建序号表
    assert controller._meta
    assert "已暂停" in await send(controller, "/off 1")
    worker = list(control.engine.workers.values())[0]
    assert worker.paused is True
    assert "已恢复" in await send(controller, "/on 1")
    assert worker.paused is False


async def test_index_auto_refreshes_when_stale(bot) -> None:
    """/off 1 没先发 /status 也能用（自动刷新序号表）。"""
    controller, control, _ = bot
    controller._meta.clear()
    reply = await send(controller, "/off 1")
    assert "已暂停" in reply


async def test_bad_index_is_reported(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/off 99")
    assert "❌" in reply
    assert "没有序号" in reply


async def test_del_removes_target(bot) -> None:
    controller, control, _ = bot
    reply = await send(controller, "/del 群A")
    assert "已移除" in reply
    assert control.engine.worker_of(-1002001) is None


async def test_check_target(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/check 群A")
    assert "能否发言" in reply
    assert "30s" in reply  # 慢速


async def test_logs_empty(bot) -> None:
    controller, *_ = bot
    assert "日志为空" in await send(controller, "/logs")


async def test_logs_tail(bot) -> None:
    controller, control, _ = bot
    control.log_path.write_text("a\nb\nc\nd\n", encoding="utf-8")
    reply = await send(controller, "/logs 2")
    assert "c" in reply and "d" in reply


async def test_run_triggers_cycle(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/run")
    assert "跑完一轮" in reply


async def test_stop_repost(bot) -> None:
    controller, control, _ = bot
    reply = await send(controller, "/stop")
    assert "停止" in reply
    assert control.reposter._stop.is_set()


async def test_internal_error_is_reported_not_raised(bot, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令内部炸了也要变成一条回复，不能让 bot 静默死掉。"""
    controller, *_ = bot

    async def boom(_: str) -> str:
        raise RuntimeError("模拟故障")

    monkeypatch.setattr(controller, "_cmd_status", boom)
    reply = await send(controller, "/status")
    assert "内部错误" in reply
    assert "模拟故障" in reply


async def test_control_error_is_reported_as_user_error(bot) -> None:
    controller, *_ = bot
    reply = await send(controller, "/add ")  # 缺参数
    assert "❌" in reply


async def test_html_is_escaped(bot) -> None:
    """用户输入进 HTML 回复前必须转义，否则会破坏消息或注入。"""
    controller, *_ = bot
    reply = await send(controller, "/limit 1 <b>1</b> 5")
    assert "❌" in reply  # 解析失败
    assert "<b>1</b>" not in reply
