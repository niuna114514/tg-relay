"""端到端 smoke：用假源消息跑通"监听 -> 过滤 -> 转发 -> 落库"。

不联网、不需要协议号，验证命令：python tests/smoke_relay.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, Target  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402

SOURCE = -1001234567890
TARGETS = (-1001111111111, -1002222222222, -1003333333333)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            sources=(SOURCE,),
            targets=tuple(Target(id=item) for item in TARGETS),
            filters=Filters(keywords=("新品",)),
            rate=Rate(
                per_target_interval=(0.05, 0.1),
                cross_target_delay=(0.1, 0.2),
                global_per_minute=600,
                daily_cap=100,
            ),
            behavior=Behavior(queue_size=50, album_window=0.1),
        )
        store = Store(Path(tmp) / "relay.db")
        client = FakeClient()
        sender = Sender(client, config, store)
        engine = RelayEngine(config, store, sender)
        engine.attach(client)
        await engine.start()

        # 1) 命中关键词的纯文本
        await engine.handle_message(
            fake_message(msg_id=1, chat_id=SOURCE, text="新品上架：限定款"), source_peer="src"
        )
        # 2) 不命中关键词，应被过滤
        await engine.handle_message(
            fake_message(msg_id=2, chat_id=SOURCE, text="今天的天气不错"), source_peer="src"
        )
        # 3) 相册（5 张图 + 文案），应整组转发
        album = [
            fake_message(msg_id=10 + i, chat_id=SOURCE, kind="photo", text="新品实拍", grouped_id=555)
            for i in range(5)
        ]
        await engine.handle_album(album, "src")
        # 4) 重复消息，应被去重拦截
        await engine.handle_message(
            fake_message(msg_id=1, chat_id=SOURCE, text="新品上架：限定款"), source_peer="src"
        )

        for _ in range(60):
            if all(worker.queue.empty() for worker in engine.workers.values()):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        await engine.stop()

        print("转发调用明细（目标, 源消息ID, from_peer）:")
        for entity, ids, peer in client.sent:
            print(f"  {entity}  {ids}  {peer}")

        expect_calls = 2 * 3  # 1 条单发 + 1 组相册，各投给 3 个目标
        report = engine.report()
        print("\n统计:", report["engine"])
        print("发送:", {k: report["sender"][k] for k in ("sent", "failed", "skipped", "flood_waits")})
        print("库:", report["store"])

        problems: list[str] = []
        if len(client.sent) != expect_calls:
            problems.append(f"期望 {expect_calls} 次转发调用，实际 {len(client.sent)}")
        if report["engine"]["duplicates"] != 1:
            problems.append("重复消息没有被去重")
        if report["engine"]["filtered"] != 1:
            problems.append("无关消息没有被过滤")
        # 配额按"帖子数"计：3 个目标各收到 1 条单发 + 1 组相册 = 6
        if report["store"]["sent_today"] != expect_calls:
            problems.append(f"每日计数与帖子数不一致：{report['store']['sent_today']} != {expect_calls}")
        album_delivery = store.find_delivery("-1001234567890:10:album5", -1001111111111)
        if album_delivery is None or album_delivery.status != "sent":
            problems.append("相册没有整组投递成功")
        if album_delivery is not None and len(album_delivery.target_msg_ids) != 5:
            problems.append("相册在目标群里被拆成了多条")
        if not all(peer == "src" for _, _, peer in client.sent):
            problems.append("forward 时没有带上来源 peer（会丢失来源标记）")

        store.close()
        if problems:
            print("\n[FAIL]")
            for item in problems:
                print("  -", item)
            return 1
        print("\n[OK] 转发链路自检通过")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
