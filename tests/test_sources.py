"""源频道管理测试：追加源、换主源、移除源、监听器热更新。

"主源"的定义：repost 素材、--probe-send 探测、历史补漏游标都以它为准。
换源时必须提醒旧素材可能失效（msg_id 是频道内编号，换频道就对不上了）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target, load_config  # noqa: E402
from tgrelay.config_store import ConfigStore  # noqa: E402
from tgrelay.control import ControlError, RuntimeControl  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402

CONFIG = """\
sources:
  - "@old_channel"
targets:
  - id: -1001
    label: 群A
rate:
  per_target_interval: [0, 0]
  cross_target_delay: [0, 0]
  global_per_minute: 6000
  daily_cap: 100
behavior:
  queue_size: 10
repost:
  enabled: true
  ids: [6]
  interval: 300
  daily_limit: 200
"""


class SourceClient(FakeClient):
    """能解析源频道，并让 get_messages 按频道返回不同内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages_calls: list[object] = []

    async def get_entity(self, peer: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=abs(hash(str(peer))) % 10_000_000,
            title=f"频道{peer}",
            username=str(peer).lstrip("@"),
            default_banned_rights=None,
            slowmode_seconds=None,
        )

    async def get_messages(self, peer: object, ids: object = None, **kwargs: object) -> object:
        self.messages_calls.append(peer)
        if ids is None:
            return [fake_message(msg_id=1, chat_id=-9001, text="最新一条")]
        pairs = ids if isinstance(ids, (list, tuple)) else [ids]
        # 模拟：只有 @old_channel 有 msg_id=6，新源没有
        out = []
        for item in pairs:
            msg_id = int(item)
            if "@old" in str(peer) or "old" in str(peer):
                out.append(fake_message(msg_id=msg_id, chat_id=-9001, text=f"旧源素材{msg_id}"))
            else:
                out.append(None)  # 新源里取不到
        return out


class FakeListener:
    """假的 ManagedListener：只记录被通知的源列表。"""

    def __init__(self) -> None:
        self.reloads: list[list[str]] = []
        self.flushed = False

    async def reload_sources(self, sources: list[object]) -> None:
        self.reloads.append([str(item) for item in sources])

    async def flush(self) -> None:
        self.flushed = True


def build(tmp_path: Path, *, with_listener: bool = True):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    config = load_config(path)
    store = Store(tmp_path / "relay.db")
    client = SourceClient()
    sender = Sender(client, config, store)
    engine = RelayEngine(config, store, sender)
    engine.attach(client)

    reposter = Reposter(config, store, sender, client=client)
    reposter.source_peer = "@old_channel"
    reposter.source_id = -9001
    reposter.messages = [fake_message(msg_id=6, chat_id=-9001, text="旧源素材6")]
    reposter._order = [0]

    listener = FakeListener() if with_listener else None
    control = RuntimeControl(
        config,
        store,
        sender,
        engine,
        config_store=ConfigStore(path),
        reposter=reposter,
        client=client,
        listener=listener,
        log_path=tmp_path / "relay.log",
        config_path=path,
    )
    return control, engine, store, client, reposter, listener, path


# --------------------------------------------------------------------------
# 列出源
# --------------------------------------------------------------------------


def test_sources_info_marks_primary(tmp_path: Path) -> None:
    control, *_ = build(tmp_path)
    rows = control.sources_info()
    assert rows == [{"peer": "@old_channel", "主源": True}]


def test_sources_info_after_adding(tmp_path: Path) -> None:
    control, *_ = build(tmp_path)
    import asyncio

    asyncio.run(control.add_source("@extra", verify=False))
    rows = control.sources_info()
    assert len(rows) == 2
    assert rows[0]["主源"] is True
    assert rows[1] == {"peer": "@extra", "主源": False}


# --------------------------------------------------------------------------
# 追加源
# --------------------------------------------------------------------------


async def test_add_source_updates_config_and_file(tmp_path: Path) -> None:
    control, _, store, _, _, listener, path = build(tmp_path)
    try:
        result = await control.add_source("@extra", verify=False)
        assert result["added"] == "@extra"
        assert control.config.sources == ("@old_channel", "@extra")
        assert "@extra" in path.read_text(encoding="utf-8")
        reloaded = load_config(path)
        assert reloaded.sources == ("@old_channel", "@extra")
        # 监听器被通知热更新
        assert listener.reloads[-1] == ["@old_channel", "@extra"]
    finally:
        store.close()


async def test_add_source_rejects_duplicate(tmp_path: Path) -> None:
    control, _, store, *_ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="已存在"):
            await control.add_source("@old_channel", verify=False)
    finally:
        store.close()


async def test_add_source_rejects_empty(tmp_path: Path) -> None:
    control, _, store, *_ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="不能为空"):
            await control.add_source("   ", verify=False)
    finally:
        store.close()


async def test_add_source_does_not_touch_materials(tmp_path: Path) -> None:
    """追加源不影响主源，素材不该被重载。"""
    control, _, store, _, reposter, _, _ = build(tmp_path)
    try:
        before = list(reposter.messages)
        await control.add_source("@extra", verify=False)
        assert reposter.messages == before
    finally:
        store.close()


# --------------------------------------------------------------------------
# 换主源
# --------------------------------------------------------------------------


async def test_switch_source_becomes_primary(tmp_path: Path) -> None:
    control, _, store, _, _, listener, path = build(tmp_path)
    try:
        result = await control.switch_source("@new_channel", verify=False)
        assert result["old"] == "@old_channel"
        assert result["source"] == "@new_channel"
        # 新源在最前 = 主源
        assert control.config.sources[0] == "@new_channel"
        assert control.config.source == "@new_channel"
        # 旧源保留在列表里（仍会被监听），去重
        assert control.config.sources == ("@new_channel", "@old_channel")
        reloaded = load_config(path)
        assert reloaded.source == "@new_channel"
    finally:
        store.close()


async def test_switch_source_warns_about_stale_materials(tmp_path: Path) -> None:
    """核心提示：新源里取不到旧素材的 msg_id，必须告诉用户去重设。

    msg_id 是频道内编号，换频道后基本一定对不上 —— 这是换源最容易踩的坑。
    """
    control, _, store, _, reposter, _, _ = build(tmp_path)
    try:
        # 注意：这里换的是 get_entity，而不是 get_messages。
        # 因为 FakeClient.get_input_entity 会把 entity 原样传下去（真实 Telethon 会
        # 返回 InputPeerChannel，repr 里只有数字 ID），所以按 peer 字符串判断"是哪个源"
        # 不可靠 —— 换个更贴近真实的建模：让 get_entity 直接决定当前是哪个源。
        async def entity_of_new(peer: object) -> object:
            from types import SimpleNamespace

            return SimpleNamespace(
                id=777, title="新频道", username="new_channel",
                default_banned_rights=None, slowmode_seconds=None,
            )

        async def fetch(peer: object, ids: object = None, **kw: object) -> object:
            pairs = ids if isinstance(ids, (list, tuple)) else [ids]
            # 新源（id=777）里没有旧素材的 msg_id
            return [
                fake_message(msg_id=int(i), chat_id=777, text="新源素材")
                if getattr(peer, "id", None) != 777
                else None
                for i in pairs
            ]

        reposter.client.get_entity = entity_of_new  # type: ignore[assignment]
        reposter.client.get_messages = fetch  # type: ignore[assignment]
        result = await control.switch_source("@new_channel", verify=False)

        assert result["materials_ok"] is False, result
        assert "取不到" in result["materials_note"] or "重设" in result["materials_note"]
        assert reposter.messages == []  # 素材确实失效了
    finally:
        store.close()

async def test_switch_source_keeps_materials_when_still_valid(tmp_path: Path) -> None:
    """如果新源里素材仍有效（比如只是改了别名），不该误报。"""
    control, _, store, _, reposter, _, _ = build(tmp_path)
    try:
        # 让 get_messages 对任何源都返回消息
        async def always(peer: object, ids: object = None, **kw: object) -> object:
            pairs = ids if isinstance(ids, (list, tuple)) else [ids]
            return [fake_message(msg_id=int(i), chat_id=-9001, text=f"素材{i}") for i in pairs]

        reposter.client.get_messages = always  # type: ignore[assignment]
        result = await control.switch_source("@alias_channel", verify=False)
        assert result["materials_ok"] is True
        assert result["materials_note"] == ""
    finally:
        store.close()


async def test_switch_to_same_source_is_idempotent(tmp_path: Path) -> None:
    control, _, store, _, _, _, _ = build(tmp_path)
    try:
        result = await control.switch_source("@old_channel", verify=False)
        assert control.config.sources == ("@old_channel",)
        assert result["source"] == "@old_channel"
    finally:
        store.close()


async def test_switch_source_empty_is_rejected(tmp_path: Path) -> None:
    control, _, store, *_ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="不能为空"):
            await control.switch_source("  ", verify=False)
    finally:
        store.close()


async def test_switch_source_notifies_listener(tmp_path: Path) -> None:
    control, _, store, _, _, listener, _ = build(tmp_path)
    try:
        await control.switch_source("@new_channel", verify=False)
        assert listener.reloads, "监听器应该被通知换源"
        assert listener.reloads[-1][0] == "@new_channel"
    finally:
        store.close()


# --------------------------------------------------------------------------
# 移除源
# --------------------------------------------------------------------------


async def test_remove_source(tmp_path: Path) -> None:
    control, _, store, _, _, listener, path = build(tmp_path)
    try:
        await control.add_source("@extra", verify=False)
        result = await control.remove_source("@extra")
        assert result["removed"] == "@extra"
        assert control.config.sources == ("@old_channel",)
        assert listener.reloads[-1] == ["@old_channel"]
    finally:
        store.close()


async def test_cannot_remove_last_source(tmp_path: Path) -> None:
    """至少要保留一个源，否则没有东西可转发。"""
    control, _, store, *_ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="至少要保留一个"):
            await control.remove_source("@old_channel")
    finally:
        store.close()


async def test_remove_unknown_source(tmp_path: Path) -> None:
    control, _, store, *_ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="找不到"):
            await control.remove_source("@nope")
    finally:
        store.close()


async def test_operations_work_without_listener(tmp_path: Path) -> None:
    """--repost-only 模式没有监听器，换源不该报错。"""
    control, _, store, _, _, listener, _ = build(tmp_path, with_listener=False)
    try:
        assert listener is None
        result = await control.switch_source("@new_channel", verify=False)
        assert result["source"] == "@new_channel"
        await control.add_source("@extra", verify=False)
        await control.remove_source("@extra")
    finally:
        store.close()


# --------------------------------------------------------------------------
# 配置写回：sources 段
# --------------------------------------------------------------------------


def test_save_sources_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    store = ConfigStore(path)
    store.save_sources(("@a", "@b"))
    text = path.read_text(encoding="utf-8")
    assert text.count("sources:") == 1
    reloaded = load_config(path)
    assert reloaded.sources == ("@a", "@b")


def test_save_sources_preserves_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    store = ConfigStore(path)
    store.save_sources(("@a",))
    reloaded = load_config(path)
    # 目标、repost 不受影响
    assert reloaded.targets[0].label == "群A"
    assert reloaded.repost.ids == (6,)


def test_save_sources_does_not_accumulate_comments(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        'sources:\n  - "@x"\ntargets:\n  - id: -1001\n'
        "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    for i in range(4):
        store.save_sources((f"@s{i}",))
    text = path.read_text(encoding="utf-8")
    assert text.count("sources:") == 1
    assert load_config(path).sources == ("@s3",)
