"""鍵盤輸入群組。

資料中的五個擊奏槽位仍是固定的 0..4；使用者可選擇不同的實體按鍵來觸發。
因此切換控制群組不會改變 Track 的 stroke 設定或既有事件。
"""

from __future__ import annotations


INPUT_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "F G H J K": (
        ("f", "F"),
        ("g", "G"),
        ("h", "H"),
        ("j", "J"),
        ("k", "K"),
    ),
    "空白鍵＋方向鍵": (
        ("space", "SPACE"),
        ("up", "↑"),
        ("down", "↓"),
        ("left", "←"),
        ("right", "→"),
    ),
}

DEFAULT_INPUT_GROUP = "F G H J K"


def normalize_group(group: str) -> str:
    return group if group in INPUT_GROUPS else DEFAULT_INPUT_GROUP


def physical_keys(group: str) -> tuple[str, ...]:
    return tuple(item[0] for item in INPUT_GROUPS[normalize_group(group)])


def display_keys(group: str) -> tuple[str, ...]:
    return tuple(item[1] for item in INPUT_GROUPS[normalize_group(group)])


def input_slot(group: str, keysym: str) -> int | None:
    """由 Tk 的 keysym 找到 0..4 的擊奏槽位。"""
    key = keysym.lower()
    try:
        return physical_keys(group).index(key)
    except ValueError:
        return None

