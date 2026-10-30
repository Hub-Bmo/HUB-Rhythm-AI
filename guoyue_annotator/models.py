"""可直接序列化為 JSON 的資料模型工廠。

這裡不用 ORM 或難以追蹤的框架，所有資料都是一般的 dict/list。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from .timeline import PPQ
from .controls import DEFAULT_INPUT_GROUP


SCHEMA_VERSION = "2.1"


BRUSH_COLORS = ["#D65A4A", "#D99536", "#3E9B75", "#3978B8", "#7A5BB5", "#B34B83"]

INSTRUMENT_PRESETS: dict[str, dict] = {
    "鼓": {
        "sound_profile": "drum",
        "brushes": [
            ("center", "鼓心"), ("rim", "鼓邊"), ("muted", "悶音"),
        ],
    },
    "大鑼": {
        "sound_profile": "large_gong",
        "brushes": [
            ("open", "開音"), ("muted", "悶音"), ("light", "輕擊"),
            ("stop", "止音"),
        ],
    },
    "小鑼": {
        "sound_profile": "small_gong",
        "brushes": [
            ("open", "開音"), ("muted", "悶音"), ("light", "輕擊"),
        ],
    },
    "鈸": {
        "sound_profile": "cymbal",
        "brushes": [
            ("clash", "合鈸"), ("muted", "悶音"), ("light", "輕擊"),
        ],
    },
    "響盞": {
        "sound_profile": "bell",
        "brushes": [
            ("open", "開音"), ("muted", "止音"), ("light", "輕擊"),
        ],
    },
    "定音鼓": {
        "sound_profile": "timpani",
        "brushes": [
            ("pitch_1", "音 1"), ("pitch_2", "音 2"),
            ("pitch_3", "音 3"), ("pitch_4", "音 4"),
            ("pitch_5", "音 5"), ("pitch_6", "音 6"),
            ("pitch_7", "音 7"),
        ],
    },
    "排鼓": {
        "sound_profile": "drum",
        "brushes": [
            ("drum_1", "鼓 1"), ("drum_2", "鼓 2"),
            ("drum_3", "鼓 3"), ("drum_4", "鼓 4"),
            ("drum_5", "鼓 5"),
        ],
    },
}

DEFAULT_TRACK_PRESETS = ["鼓", "大鑼", "小鑼", "鈸", "響盞", "定音鼓"]

KEYS = ["f", "g", "h", "j", "k"]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _annotation_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def ensure_event_annotations(event: dict) -> dict:
    """補齊每個鼓點獨立於敲法的布林標記。"""
    event["is_accent"] = _annotation_flag(event.get("is_accent", False))
    event["is_ornament"] = _annotation_flag(event.get("is_ornament", False))
    legacy_roll = "roll" in str(event.get("stroke", "")).lower()
    event["is_roll"] = _annotation_flag(event.get("is_roll", legacy_roll))
    return event


def ensure_project_event_annotations(project: dict) -> None:
    for segment in project.get("segments", []):
        for track in segment.get("tracks", []):
            for take in track.get("takes", []):
                for event in take.get("events", []):
                    ensure_event_annotations(event)


def _brush_records(brushes: list[tuple[str, str]]) -> list[dict]:
    return [
        {"code": code, "label": label, "color": BRUSH_COLORS[index % len(BRUSH_COLORS)]}
        for index, (code, label) in enumerate(brushes)
    ]


def make_track(
    segment_id: str,
    order: int,
    spec: tuple | None = None,
    preset_name: str | None = None,
) -> dict:
    if preset_name in INSTRUMENT_PRESETS:
        instrument = str(preset_name)
        preset = INSTRUMENT_PRESETS[instrument]
        profile = preset["sound_profile"]
        brush_pairs = list(preset["brushes"])
    elif spec is not None:
        instrument, profile, strokes = spec
        brush_pairs = [(stroke, stroke) for stroke in strokes]
    else:
        instrument = f"樂器 {order}"
        profile = "bell"
        brush_pairs = [
            ("open", "開音"), ("muted", "悶音"), ("light", "輕擊"),
        ]
    brushes = _brush_records(brush_pairs)
    return {
        "track_id": f"{segment_id}-T{order:02d}",
        "segment_id": segment_id,
        "instrument": instrument,
        "sound_profile": profile,
        "track_order": order,
        "is_complete": False,
        "selected_take_id": None,
        "notes": "",
        "keymap": dict(zip(KEYS, [brush["code"] for brush in brushes[:5]])),
        "brushes": brushes,
        "takes": [],
    }


def new_segment(segment_id: str = "BG0001") -> dict:
    """建立一段預設包含五件樂器的合奏片段。"""
    return {
        "segment_id": segment_id,
        "piece_id": "P0001",
        "piece_name": "",
        "pattern_name": "",
        "section_name": "",
        "selection_type": "基本型",
        "selection_reason": "",
        "source": "",
        "source_page": "",
        "source_start_bar": "",
        "source_end_bar": "",
        "system": "",
        "time_signature": "4/4",
        "tempo": 90,
        "bars": 4,
        "ppq": PPQ,
        "subdivision": "1/16",
        "version": "1.0",
        "notes": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "bar_settings": [
            {"bar": index, "time_signature": "4/4", "subdivision": "1/16"}
            for index in range(1, 5)
        ],
        # Source regions keep the score-image relationship outside Event records,
        # so the canonical training schema stays stable and portable.
        "source_refs": [],
        "navigation": {
            "markers": [],
            "playback_order": [],
            "review_status": "not_started",
        },
        "review": {
            "status": "not_started",
            "by_track": {},
            "updated_at": None,
        },
        "tracks": [
            make_track(segment_id, index, preset_name=preset_name)
            for index, preset_name in enumerate(DEFAULT_TRACK_PRESETS, start=1)
        ],
    }


def new_project() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ppq": PPQ,
        "input_mode": "mixed",
        "input_group": DEFAULT_INPUT_GROUP,
        "hit_sound_enabled": True,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "next_event_number": 1,
        "assets": [],
        "recognition_runs": [],
        "score_plan": {
            "version": "1.0",
            "status": "not_started",
            "page_asset_ids": [],
            "page_order_confirmed": False,
            "excluded_asset_ids": [],
            "analyses_by_asset": {},
            "measure_sequence": [],
            "segment_proposals": [],
            "updated_at": None,
        },
        "instrument_library_snapshot": None,
        "workspace_state": {
            "photo_asset_id": None,
            "photo_zoom": 1.0,
            "photo_scroll": [0.0, 0.0],
        },
        "segments": [new_segment()],
    }


def next_segment_id(project: dict) -> str:
    used_numbers = []
    for segment in project.get("segments", []):
        value = segment.get("segment_id", "")
        if value.startswith("BG") and value[2:].isdigit():
            used_numbers.append(int(value[2:]))
    return f"BG{max(used_numbers, default=0) + 1:04d}"


def next_take_id(track: dict) -> str:
    used_numbers = []
    for take in track.get("takes", []):
        value = take.get("take_id", "")
        tail = value.rsplit("-K", 1)[-1]
        if tail.isdigit():
            used_numbers.append(int(tail))
    return f"{track['track_id']}-K{max(used_numbers, default=0) + 1:03d}"


def next_event_id(project: dict) -> str:
    number = int(project.setdefault("next_event_number", 1))
    project["next_event_number"] = number + 1
    return f"E{number:07d}"


def duplicate_segment_structure(project: dict, source: dict) -> dict:
    """複製背景欄位與軌道設定，但不複製 Take/Event。"""
    segment_id = next_segment_id(project)
    result = deepcopy(source)
    result["segment_id"] = segment_id
    result["created_at"] = now_iso()
    result["updated_at"] = now_iso()
    result["source_refs"] = []
    result["navigation"] = {
        "markers": [],
        "playback_order": [],
        "review_status": "not_started",
    }
    result["review"] = {
        "status": "not_started",
        "by_track": {},
        "updated_at": None,
    }
    for index, track in enumerate(result["tracks"], start=1):
        track["track_id"] = f"{segment_id}-T{index:02d}"
        track["segment_id"] = segment_id
        track["track_order"] = index
        track["is_complete"] = False
        track["selected_take_id"] = None
        track["takes"] = []
    return result


def selected_take(track: dict) -> dict | None:
    selected_id = track.get("selected_take_id")
    return next(
        (take for take in track.get("takes", []) if take["take_id"] == selected_id),
        None,
    )
