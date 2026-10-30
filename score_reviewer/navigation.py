"""B 的全曲路線：沿用一般反覆，另明確記錄一次性的 D.C.／Coda。

不從符號自行猜測 Coda 配對或返回後是否重做反覆；這些都是人工決定。
"""
from copy import deepcopy
import re

from guoyue_annotator.navigation import (
    _compile_navigation, expand_playback_order as expand_repeats,
)
from score_recognizer.contract import ContractError

JUMP_KINDS = {"dc", "dc_coda"}


def marker_range(marker):
    if marker.get("type") in JUMP_KINDS:
        return 1, max(marker.get("bar", 0), marker.get("target_bar", 0))
    return (marker.get("bar", marker.get("start_bar")),
            marker.get("end_bar", marker.get("bar", marker.get("start_bar"))))


def expand_playback_order(bar_count, markers):
    markers = list(markers)
    jumps = [m for m in markers if m.get("type") in JUMP_KINDS]
    ordinary = [m for m in markers if m.get("type") not in JUMP_KINDS]
    base = expand_repeats(bar_count, ordinary)
    if not jumps:
        return base
    if len(jumps) != 1:
        raise ContractError("目前一首支援一組 D.C. 返回；請勿重複加入跳轉")
    jump = jumps[0]
    fields = ["bar"] + (["until_bar", "target_bar"] if jump["type"] == "dc_coda" else [])
    if any(type(jump.get(key)) is not int or not 1 <= jump[key] <= bar_count for key in fields):
        raise ContractError("D.C.／Coda 的小節必須是已登記的整數小節號")
    if jump.get("repeat_policy") not in {"skip", "play"}:
        raise ContractError("請明確選擇返回後是否重做一般反覆")
    dc_bar = jump["bar"]
    until = jump.get("until_bar", bar_count)
    target = jump.get("target_bar")
    if jump["type"] == "dc_coda" and not until < dc_bar < target:
        raise ContractError("Coda 路線須為：回奏終點 < D.C. 所在小節 < 尾奏起點")
    sections = _compile_navigation(bar_count, ordinary)
    # 不在反覆／結尾中途切開路線，避免丟失 pass 或重做半組反覆。
    cuts = [dc_bar] + ([until, target - 1] if target else [])
    if any(s.start_bar <= cut < s.logical_end_bar for s in sections for cut in cuts):
        raise ContractError("跳轉落在一般反覆或不同結尾的中途；請先核對完整路線")
    returned = base
    if jump["repeat_policy"] == "skip":
        # 不重做反覆時使用最後一次結尾，不會把第一、第二結尾都演奏一次。
        returned = [item for item in base if all(
            not s.start_bar <= item["source_bar"] <= s.logical_end_bar
            or item["pass_index"] == s.times for s in sections)]
    phases = [([item for item in base if item["source_bar"] <= dc_bar], 0),
              ([item for item in returned if item["source_bar"] <= until], 1)]
    if target:
        phases.append(([item for item in returned if item["source_bar"] >= target], 2))
    return [{**deepcopy(item), "route_phase": phase} for items, phase in phases for item in items]


def route_summary(markers):
    jumps = [m for m in markers if m.get("type") in JUMP_KINDS]
    if not jumps:
        return ""
    m = jumps[0]
    policy = "重做一般反覆" if m.get("repeat_policy") == "play" else "略過一般反覆、採最後結尾"
    route = f"至 {m['bar']} 末 → D.C. 回 1"
    if m["type"] == "dc_coda":
        route += f" → 回奏至 {m['until_bar']} 末 → Coda 跳至 {m['target_bar']} 開始"
    return route + f"；返回後{policy}。"


def evidence_resolved(candidate, markers):
    """只配對明確的 D.C./Coda 原文與人工確認位置；D.S./Fine 等仍阻擋匯出。"""
    raw = re.sub(r"[\s.]", "", candidate.get("raw_text", "")).casefold()
    bar = candidate["start_bar"]
    for m in markers:
        if m.get("type") not in JUMP_KINDS:
            continue
        if raw == "dc" and bar == m["bar"]:
            return True
        if m["type"] == "dc_coda":
            if raw == "dcalcoda" and bar == m["bar"]:
                return True
            if raw == "tocoda" and bar == m["until_bar"]:
                return True
            if raw in {"coda", "𝄌", "⊕"} and bar in {m["until_bar"], m["until_bar"] + 1, m["target_bar"]}:
                return True
    return False
