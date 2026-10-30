"""格狀輸入與可變拍號時間軸。

介面上的格子只是輸入解析度；正式資料一律換算成 PPQ=960 的絕對 tick。
每一小節可有自己的拍號與細分，因此能處理變拍號與三連音。
"""

from __future__ import annotations

from copy import deepcopy

from .timeline import PPQ, parse_time_signature, ticks_per_bar, ticks_per_beat


SUBDIVISION_TICKS: dict[str, int] = {
    "1/8": PPQ // 2,
    "1/16": PPQ // 4,
    "1/32": PPQ // 8,
    "1/8 triplet": PPQ // 3,
    "1/16 triplet": PPQ // 6,
}

SUBDIVISION_LABELS: dict[str, str] = {
    "1/8": "八分音符｜2 格／四分拍",
    "1/16": "十六分音符｜4 格／四分拍",
    "1/32": "三十二分音符｜8 格／四分拍",
    "1/8 triplet": "八分三連音｜3 格／四分拍",
    "1/16 triplet": "十六分三連音｜6 格／四分拍",
}

LABEL_TO_SUBDIVISION = {label: code for code, label in SUBDIVISION_LABELS.items()}

COMMON_TIME_SIGNATURES = ("4/4", "3/4", "2/4", "6/8", "3/8", "5/4", "7/8", "9/8", "12/8")


def normalize_subdivision(value: str | None) -> str:
    if value in SUBDIVISION_TICKS:
        return str(value)
    if value in LABEL_TO_SUBDIVISION:
        return LABEL_TO_SUBDIVISION[str(value)]
    return "1/16"


def subdivision_label(value: str | None) -> str:
    return SUBDIVISION_LABELS[normalize_subdivision(value)]


def ensure_bar_settings(segment: dict) -> list[dict]:
    """補齊舊專案沒有的逐小節設定，並與 ``bars`` 數量同步。"""
    bars = max(1, int(segment.get("bars", 1)))
    default_signature = str(segment.get("time_signature", "4/4"))
    try:
        parse_time_signature(default_signature)
    except ValueError:
        default_signature = "4/4"
    default_subdivision = normalize_subdivision(segment.get("subdivision"))

    existing = segment.get("bar_settings")
    if not isinstance(existing, list):
        existing = []
    result: list[dict] = []
    for index in range(bars):
        source = existing[index] if index < len(existing) and isinstance(existing[index], dict) else {}
        signature = str(source.get("time_signature", default_signature))
        try:
            parse_time_signature(signature)
        except ValueError:
            signature = default_signature
        result.append({
            "bar": index + 1,
            "time_signature": signature,
            "subdivision": normalize_subdivision(source.get("subdivision", default_subdivision)),
        })
    segment["bar_settings"] = result
    segment["bars"] = bars
    if result:
        segment["time_signature"] = result[0]["time_signature"]
        segment["subdivision"] = result[0]["subdivision"]
    return result


def ensure_track_brushes(track: dict) -> list[dict]:
    brushes = track.get("brushes")
    if isinstance(brushes, list) and brushes:
        normalized = []
        fallback_colors = ["#D65A4A", "#D99536", "#3E9B75", "#3978B8", "#7A5BB5", "#B34B83"]
        for index, brush in enumerate(brushes[:9]):
            if isinstance(brush, dict):
                code = str(brush.get("code") or f"stroke_{index + 1}")
                label = str(brush.get("label") or code)
                color = str(brush.get("color") or fallback_colors[index % len(fallback_colors)])
                stroke_id = brush.get("stroke_id")
            else:
                code = str(brush)
                label = code
                color = fallback_colors[index % len(fallback_colors)]
                stroke_id = None
            normalized.append({
                "code": code,
                "label": label,
                "color": color,
                "stroke_id": stroke_id,
            })
        track["brushes"] = normalized
        return normalized

    keymap = track.get("keymap", {})
    strokes = list(dict.fromkeys(str(value) for value in keymap.values()))
    if not strokes:
        strokes = ["open", "muted", "light", "accent", "roll"]
    labels = {
        "center": "鼓心", "rim": "鼓邊", "muted": "悶音", "accent": "重擊",
        "roll": "滾奏", "open": "開音", "light": "輕擊", "clash": "合鈸",
        "stop": "止音",
    }
    fallback_colors = ["#D65A4A", "#D99536", "#3E9B75", "#3978B8", "#7A5BB5", "#B34B83"]
    track["brushes"] = [
        {
            "code": stroke,
            "label": labels.get(stroke, stroke),
            "color": fallback_colors[index % len(fallback_colors)],
            "stroke_id": None,
        }
        for index, stroke in enumerate(strokes[:9])
    ]
    return track["brushes"]


def grid_ticks(subdivision: str) -> int:
    return SUBDIVISION_TICKS[normalize_subdivision(subdivision)]


def grid_cell_count(time_signature: str, subdivision: str) -> int:
    per_bar = ticks_per_bar(time_signature)
    step = grid_ticks(subdivision)
    if per_bar % step:
        raise ValueError("此拍號無法整除目前格線，請改用其他細分")
    return per_bar // step


def bar_start_ticks(segment: dict) -> list[int]:
    starts: list[int] = []
    current = 0
    for setting in ensure_bar_settings(segment):
        starts.append(current)
        current += ticks_per_bar(setting["time_signature"])
    return starts


def segment_total_ticks(segment: dict) -> int:
    settings = ensure_bar_settings(segment)
    return sum(ticks_per_bar(setting["time_signature"]) for setting in settings)


def bar_range_ticks(segment: dict, bar_number: int) -> tuple[int, int]:
    settings = ensure_bar_settings(segment)
    if not 1 <= bar_number <= len(settings):
        raise IndexError("小節編號超出範圍")
    start = bar_start_ticks(segment)[bar_number - 1]
    end = start + ticks_per_bar(settings[bar_number - 1]["time_signature"])
    return start, end


def grid_event_tick(segment: dict, bar_number: int, cell_index: int) -> int:
    settings = ensure_bar_settings(segment)
    setting = settings[bar_number - 1]
    count = grid_cell_count(setting["time_signature"], setting["subdivision"])
    if not 0 <= cell_index < count:
        raise IndexError("格子編號超出小節範圍")
    start, _ = bar_range_ticks(segment, bar_number)
    return start + cell_index * grid_ticks(setting["subdivision"])


def tick_to_variable_bar_beat(segment: dict, tick: int) -> tuple[int, float]:
    settings = ensure_bar_settings(segment)
    tick = max(0, int(tick))
    starts = bar_start_ticks(segment)
    for index, setting in enumerate(settings):
        start = starts[index]
        end = start + ticks_per_bar(setting["time_signature"])
        if tick < end or index == len(settings) - 1:
            beat_ticks = ticks_per_beat(setting["time_signature"])
            beat = (min(tick, end) - start) / beat_ticks + 1
            return index + 1, round(beat, 6)
    return len(settings), 1.0


def event_cell_index(segment: dict, event: dict) -> int | None:
    bar = int(event.get("bar", 1))
    settings = ensure_bar_settings(segment)
    if not 1 <= bar <= len(settings):
        bar, _ = tick_to_variable_bar_beat(segment, int(event.get("tick", 0)))
    start, end = bar_range_ticks(segment, bar)
    tick = int(event.get("tick", start))
    if not start <= tick < end:
        return None
    step = grid_ticks(settings[bar - 1]["subdivision"])
    return int(round((tick - start) / step))


def reflow_events_after_settings_change(segment: dict, old_settings: list[dict]) -> None:
    """拍號或格線改變後，保留每個事件在原小節中的相對位置。"""
    if not old_settings:
        return
    old_segment = {
        "bars": len(old_settings),
        "time_signature": old_settings[0]["time_signature"],
        "subdivision": old_settings[0]["subdivision"],
        "bar_settings": deepcopy(old_settings),
    }
    new_settings = ensure_bar_settings(segment)
    old_starts = bar_start_ticks(old_segment)
    new_starts = bar_start_ticks(segment)

    for track in segment.get("tracks", []):
        for take in track.get("takes", []):
            remapped: dict[int, dict] = {}
            for event in take.get("events", []):
                old_tick = int(event.get("tick", 0))
                old_bar, _ = tick_to_variable_bar_beat(old_segment, old_tick)
                if old_bar > len(new_settings):
                    continue
                old_setting = old_settings[old_bar - 1]
                old_size = ticks_per_bar(old_setting["time_signature"])
                old_relative = max(0, old_tick - old_starts[old_bar - 1])
                fraction = min(0.999999, old_relative / max(1, old_size))
                new_setting = new_settings[old_bar - 1]
                new_size = ticks_per_bar(new_setting["time_signature"])
                step = grid_ticks(new_setting["subdivision"])
                cell_count = grid_cell_count(new_setting["time_signature"], new_setting["subdivision"])
                cell = min(cell_count - 1, max(0, int(fraction * new_size / step + 0.5)))
                tick = new_starts[old_bar - 1] + cell * step
                event["bar"] = old_bar
                event["beat"] = round((cell * step) / ticks_per_beat(new_setting["time_signature"]) + 1, 6)
                event["tick"] = tick
                event["raw_tick"] = tick
                event["grid_index"] = cell
                event["quantization"] = new_setting["subdivision"]
                remapped[tick] = event
            take["events"] = sorted(remapped.values(), key=lambda item: int(item["tick"]))


def page_tick_range(segment: dict, page_start_bar: int, page_size: int = 4) -> tuple[int, int]:
    settings = ensure_bar_settings(segment)
    first_bar = max(1, min(page_start_bar, len(settings)))
    last_bar = min(len(settings), first_bar + page_size - 1)
    start, _ = bar_range_ticks(segment, first_bar)
    _, end = bar_range_ticks(segment, last_bar)
    return start, end
