"""明確的段落演奏順序；每個範圍照寫出的順序奏一次，不另推測反覆。"""
import re
from collections import defaultdict

from score_recognizer.contract import ContractError, fraction, meter_length


def parse_sequence(text):
    ranges = []
    for part in re.split(r"[→>;；\n,，]+", text.strip()):
        if not part.strip():
            continue
        match = re.fullmatch(r"\s*(\d+)\s*(?:[-–—~～至]\s*(\d+))?\s*", part)
        if not match:
            raise ContractError("順序請用小節範圍，例如 153-154 → 1-8 → 1-7 → 9-17")
        ranges.append({"start_bar": int(match[1]), "end_bar": int(match[2] or match[1])})
    if not ranges:
        raise ContractError("請填至少一個小節範圍")
    return ranges


def expand_ranges(ranges, registered):
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 256:
        raise ContractError("請填 1–256 組小節範圍")
    maximum = max(map(int, registered), default=0)
    order = []
    for index, value in enumerate(ranges, 1):
        if not isinstance(value, dict):
            raise ContractError("段落順序格式無效")
        start, end = value.get("start_bar"), value.get("end_bar")
        if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= maximum
                or any(str(bar) not in registered for bar in range(start, end + 1))):
            raise ContractError("每組範圍須由小到大，且全部小節已登記；返回開頭請另寫下一組範圍")
        if len(order) + end - start + 1 > 10000:
            raise ContractError("一個段落最多 10000 個演奏小節，請拆分段落")
        order.extend({"source_bar": bar, "sequence_step": index} for bar in range(start, end + 1))
    return order


def format_sequence(ranges):
    return " → ".join(str(r["start_bar"]) if r["start_bar"] == r["end_bar"] else f"{r['start_bar']}–{r['end_bar']}" for r in ranges)


def segment_bars(segment):
    ranges = segment["ranges"] if segment.get("kind") == "sequence" else [segment]
    return {b for r in ranges for b in range(r["start_bar"], r["end_bar"] + 1)}


def segment_statistics(doc, segment):
    """計算演奏引用中的起音事件；0 與『尚無資料』分開。輪奏／和弦不拆擊。"""
    selected = segment_bars(segment)
    order_pending = False
    if segment.get("kind") == "sequence":
        order = [r["source_bar"] for r in doc.segment_order(segment)]
        order_pending = not segment.get("sequence_acknowledged")
    else:
        order_pending = any(not doc.navigation_covered(b, b) for b in selected)
        try:
            order = [r["source_bar"] for r in doc.segment_order(segment)] if not order_pending else sorted(selected)
        except ValueError:
            order, order_pending = sorted(selected), True
    by_bar = defaultdict(list)
    for entry in doc.entries():
        if entry["status"] != "rejected" and entry["value"]["bar"] in selected:
            by_bar[entry["value"]["bar"]].append(entry)
    missing, incomplete, silent = set(), set(), set()
    for bar in selected:
        source = doc.data["bars"].get(str(bar))
        entries = by_bar[bar]
        if not source or doc.data["regions"][source["region_id"]]["status"] != "succeeded" or not entries:
            missing.add(bar)
            continue
        if any(e["status"] != "accepted" or e["value"]["kind"] == "unreadable" or doc.issues(e) for e in entries):
            incomplete.add(bar)
        staffs = defaultdict(list)
        for entry in entries:
            staffs[entry["value"]["staff_label"]].append(entry["value"])
        meter, _ = doc.bar_setting(bar)
        quiet = bool(staffs and meter)
        for values in staffs.values():
            if any(v["kind"] == "note" or v["kind"] == "unreadable" for v in values):
                quiet = False
                break
            if any(v["kind"] == "full_rest" for v in values):
                continue
            try:
                intervals = sorted((fraction(v["onset"]), fraction(v["onset"]) + fraction(v["duration"], positive=True)) for v in values)
                cursor = 0
                for start, end in intervals:
                    if start > cursor:
                        break
                    cursor = max(cursor, end)
                if cursor < meter_length(meter):
                    quiet = False
            except (KeyError, TypeError, ContractError):
                quiet = False
        if quiet and bar not in incomplete:
            silent.add(bar)
    count = sum(e["value"]["kind"] == "note" and e["value"].get("tie") not in {"continue", "stop"}
                for bar in order for e in by_bar[bar])
    unknown_attacks = sum(e["value"]["kind"] == "note" and e["value"].get("tie") not in {"none", "start", "continue", "stop"}
                          for bar in order for e in by_bar[bar])
    verified_silent = bool(selected) and selected == silent
    status = []
    if missing:
        status.append(f"{len(missing)} 小節缺資料")
    if incomplete:
        status.append(f"{len(incomplete)} 小節待核對")
    if order_pending:
        status.append("演奏順序待確認")
    if unknown_attacks:
        status.append(f"{unknown_attacks} 筆起音待確認")
    if verified_silent:
        status = ["全段已核對休止"] + (["演奏順序待確認"] if order_pending else [])
    return {"event_count": count, "missing_bars": sorted(missing), "pending_bars": sorted(incomplete),
            "order_pending": order_pending, "verified_silent": verified_silent,
            "status": "；".join(status) or ("資料已核對" if count else "尚不能判定全段休止")}
