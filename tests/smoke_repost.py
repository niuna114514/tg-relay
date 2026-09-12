"""端到端 smoke：定时重发链路（素材 -> 循环 -> 配额 -> 落库）。

不联网、不需要协议号，验证命令：python tests/smoke_repost.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402

SOURCE = -1001234567890
TARGETS = (-1001111111111, -1002222222222)
MATERIAL = (6, 7, 8)  # 模拟 "t.me/example_channel/6 开始的一段"


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            sources=(SOURCE,),
            targets=tuple(Target(id=item) for item in TARGETS),
            filters=Filters(),
            rate=Rate(
                per_target_interval=(0.0, 0.0),
                cross_target_delay=(0.0, 0.0),
                global_per_minute=6000,
                daily_cap=100,
            ),
            behavior=Behavior(queue_size=50),
            repost=RepostConfig(
                enabled=True,
                ranges=((6, 8),),
                interval=31.0,
                daily_limit=10,
                shuffle=False,
            ),
        )
        store = Store(Path(tmp) / "relay.db")
        client = FakeClient()
        sender = Sender(client, config, store)
        reposter = Reposter(config, store, sender, client=client)
        reposter.messages = [
            fake_message(msg_id=i, chat_id=SOURCE, text=f"固定素材 {i}") for i in MATERIAL
        ]
        reposter._order = list(range(len(reposter.messages)))
        reposter.source_peer = "src"
        reposter.source_id = SOURCE

        print("素材:", [m.id for m in reposter.messages],
              "| 间隔:", config.repost.interval, "s | 日上限:", config.repost.daily_limit)
        print()

        ok1, bad1 = await reposter.run_cycle()
        print(f"第 1 轮：成功 {ok1}，跳过/失败 {bad1}")
        ok2, bad2 = await reposter.run_cycle()
        print(f"第 2 轮：成功 {ok2}，跳过/失败 {bad2}")
        print()

        print("转发调用明细（目标, 源消息ID, from_peer）:")
        for entity, ids, peer in client.sent:
            print(f"  {entity}  {ids}  {peer}")

        report = {
            "repost": reposter.stats.as_dict(),
            "store": store.stats(),
            "repost_counts": store.repost_counts(),
        }
        print("\n统计:", report)

        problems: list[str] = []
        per_cycle = len(MATERIAL) * len(TARGETS)  # 3 素材 x 2 目标 = 6
        cap = config.repost.daily_limit          # 10
        expected = min(per_cycle * 2, cap)       # 两轮共 12 次，被日上限卡到 10

        if len(client.sent) != expected:
            problems.append(f"应发 {expected} 次（两轮 {per_cycle * 2} 次被日上限 {cap} 卡住），实际 {len(client.sent)}")
        if reposter.stats.cycles != 2:
            problems.append("轮次计数不对")
        if store.reposted_today() != expected:
            problems.append(f"重发额度占用应为 {expected}，实际 {store.reposted_today()}")
        if store.sent_ok_today(as_repost=True) != expected:
            problems.append(f"重发实发数应为 {expected}，实际 {store.sent_ok_today(as_repost=True)}")
        if store.sent_today() != 0:
            problems.append("重发污染了实时转发的配额账")
        # sent_ok_today() 不带参数 = 所有真实发出的（含重发），所以这里要和重发实发数相等
        relay_ok = store.sent_ok_today(as_repost=False)
        if relay_ok != 0:
            problems.append(f"重发污染了实时转发的实发计数：as_repost=False -> {relay_ok}")
        if store.sent_ok_today() != store.sent_ok_today(as_repost=True):
            problems.append("总数与重发数不一致，说明混进了非重发的发送记录")
        # 6、7 各发 2 次（每轮发到两个目标），8 在第 2 轮被配额卡住只发了 2 次
        counts = store.repost_counts()
        if counts != {6: 4, 7: 4, 8: 2}:
            problems.append(f"素材重发次数不对：{counts}（应为 {{6: 4, 7: 4, 8: 2}}）")
        if sum(counts.values()) != expected:
            problems.append("成功次数与日计数不一致")
        if not all(peer == "src" for _, _, peer in client.sent):
            problems.append("重发时没带来源 peer（会丢失来源标记）")

        store.close()
        if problems:
            print("\n[FAIL]")
            for item in problems:
                print("  -", item)
            return 1
        print("\n[OK] 定时重发链路自检通过")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
