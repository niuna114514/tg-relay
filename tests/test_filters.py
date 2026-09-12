"""过滤规则测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import fake_message  # noqa: E402
from tgrelay.config import Filters  # noqa: E402
from tgrelay.filters import MessageFilter, classify  # noqa: E402


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("text", "text"),
        ("photo", "photo"),
        ("video", "video"),
        ("gif", "gif"),
        ("sticker", "sticker"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("document", "document"),
        ("poll", "poll"),
    ],
)
def test_classify(kind: str, expected: str) -> None:
    assert classify(fake_message(kind=kind)) == expected


def test_empty_filter_allows_everything() -> None:
    rule = MessageFilter(Filters())
    assert not rule.active
    assert rule.check(fake_message(text="随便什么")).allowed
    assert rule.check(fake_message(kind="photo")).allowed
    assert rule.check(fake_message(text="")).allowed


def test_keyword_any() -> None:
    rule = MessageFilter(Filters(keywords=("上新", "优惠")))
    assert rule.check(fake_message(text="今天上新啦")).allowed
    assert not rule.check(fake_message(text="今天下雨")).allowed


def test_keyword_all() -> None:
    rule = MessageFilter(Filters(keywords=("上新", "优惠"), keyword_mode="all"))
    assert rule.check(fake_message(text="上新 优惠 一起")).allowed
    decision = rule.check(fake_message(text="只有上新"))
    assert not decision.allowed
    assert "缺少关键词" in decision.reason


def test_keyword_is_case_insensitive() -> None:
    rule = MessageFilter(Filters(keywords=("SALE",)))
    assert rule.check(fake_message(text="big sale today")).allowed


def test_exclude_wins() -> None:
    rule = MessageFilter(Filters(keywords=("上新",), exclude=("广告",)))
    assert rule.check(fake_message(text="上新")).allowed
    assert not rule.check(fake_message(text="上新但是广告")).allowed


def test_regex() -> None:
    rule = MessageFilter(Filters(regex=(r"特价\s*\d+",)))
    assert rule.check(fake_message(text="特价 199")).allowed
    assert not rule.check(fake_message(text="特价")).allowed


def test_invalid_regex_raises() -> None:
    with pytest.raises(ValueError, match="正则"):
        MessageFilter(Filters(regex=("([",)))


def test_media_whitelist() -> None:
    rule = MessageFilter(Filters(media=("photo", "video")))
    assert rule.check(fake_message(kind="photo")).allowed
    assert rule.check(fake_message(kind="video")).allowed
    decision = rule.check(fake_message(kind="document", text="pdf"))
    assert not decision.allowed
    assert decision.kind == "document"


def test_min_length_only_applies_to_text() -> None:
    rule = MessageFilter(Filters(min_length=10))
    assert not rule.check(fake_message(text="短")).allowed
    assert rule.check(fake_message(text="刚好十个字符的文本内容")).allowed
    # 带媒体的消息不受 min_length 限制
    assert rule.check(fake_message(kind="photo", text="")).allowed


def test_caption_is_used_for_keywords() -> None:
    rule = MessageFilter(Filters(keywords=("新品",)))
    assert rule.check(fake_message(kind="photo", text="新品到货")).allowed
    assert not rule.check(fake_message(kind="photo", text="旧货")).allowed


def test_rules_are_and_combined() -> None:
    rule = MessageFilter(Filters(keywords=("上新",), media=("photo",)))
    assert rule.check(fake_message(kind="photo", text="上新了")).allowed
    assert not rule.check(fake_message(kind="text", text="上新了")).allowed
    assert rule.check(fake_message(kind="photo", text="上新，字数很短也放行")).allowed
