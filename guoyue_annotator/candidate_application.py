"""Safely turn reviewed AI candidates into editable rhythm data.

Recognition output is deliberately stored as review candidates first.  This
module is the explicit boundary that converts only accepted/corrected data
into a separate AI take, so an existing human take is never overwritten.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Iterable, Mapping

from .grid import (
    bar_range_ticks,
    ensure_bar_settings,
    grid_cell_count,
    grid_ticks,
)
from .instrument_library import (
    UNKNOWN_ID,
    attach_project_snapshot,
    get_instrument,
    get_stroke,
)
from .models import BRUSH_COLORS, next_event_id, next_take_id
from .timeline import ticks_per_beat


REVIEWED_STATUSES = frozenset({"accepted", "corrected"})
AI_INPUT_METHOD = "ai-recognition"


class CandidateApplicationError(ValueError):
    """Reviewed candidates cannot be applied without user correction."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return parsed


def _candidate_data(candidate: Mapping[str, object]) -> Mapping[str, object]:
    if candidate.get("review_status") == "corrected":
        review = candidate.get("review")
        if isinstance(review, Mapping) and isinstance(review.get("corrected_data"), Mapping):
            return review["corrected_data"]  # type: ignore[return-value]
    data = candidate.get("data")
    return data if isinstance(data, Mapping) else {}


def _candidate_key(run: Mapping[str, object], candidate: Mapping[str, object], index: int) -> str:
    run_id = str(run.get("run_id") or "run")
    candidate_id = str(candidate.get("candidate_id") or f"candidate-{index + 1}")
    return f"{run_id}:{candidate_id}"


def _visible_numeric_symbol(candidate: Mapping[str, object]) -> str | None:
    # Human-corrected data is authoritative; the original AI payload remains
    # only as audit history after review_status becomes ``corrected``.
    data = _candidate_data(candidate)
    evidence = candidate.get("source_evidence")
    values = []
    if isinstance(data, Mapping):
        values.append(data.get("raw_symbol"))
    if isinstance(evidence, Mapping):
        values.append(evidence.get("observed_text"))
    for value in values:
        if not isinstance(value, str):
            continue
        digits = re.findall(r"\d", value)
        if len(digits) == 1 and not re.search(r"\d\s*\d", value):
            return digits[0]
    return None


def _numbered_stroke_id(
    instrument: Mapping[str, object], symbol: str | None
) -> str | None:
    """Resolve explicit numbered controls such as pitch_6 or drum_3."""

    if symbol is None or symbol == "0":
        return None
    expected_codes = {f"pitch_{symbol}", f"drum_{symbol}"}
    matches = [
        str(stroke.get("stroke_id"))
        for stroke in instrument.get("strokes", [])  # type: ignore[union-attr]
        if isinstance(stroke, Mapping)
        and str(stroke.get("code") or "").casefold() in expected_codes
        and isinstance(stroke.get("stroke_id"), str)
    ]
    return matches[0] if len(set(matches)) == 1 else None


def _existing_candidate_keys(segment: Mapping[str, object]) -> set[str]:
    keys: set[str] = set()
    for track in segment.get("tracks", []):  # type: ignore[union-attr]
        if not isinstance(track, Mapping):
            continue
        for take in track.get("takes", []):
            if not isinstance(take, Mapping):
                continue
            for event in take.get("events", []):
                if isinstance(event, Mapping):
                    key = event.get("source_candidate_key")
                    if isinstance(key, str) and key:
                        keys.add(key)
    navigation = segment.get("navigation")
    if isinstance(navigation, Mapping):
        for marker in navigation.get("markers", []):
            if isinstance(marker, Mapping):
                key = marker.get("source_candidate_key")
                if isinstance(key, str) and key:
                    keys.add(key)
    return keys


def _track_for_instrument(segment: Mapping[str, object], instrument_id: str) -> Mapping[str, object] | None:
    matches = [
        track
        for track in segment.get("tracks", [])  # type: ignore[union-attr]
        if isinstance(track, Mapping) and track.get("instrument_id") == instrument_id
    ]
    return matches[0] if len(matches) == 1 else None


def _event_operation(
    segment: Mapping[str, object],
    run: Mapping[str, object],
    candidate: Mapping[str, object],
    candidate_index: int,
    library: Mapping[str, object],
) -> tuple[dict | None, str | None]:
    data = _candidate_data(candidate)
    is_numbered_profile = (
        run.get("detected_profile") == "numbered"
        or run.get("notation_profile") == "numbered"
    )
    # A lone digit in staff/gongjing evidence can be a printed bar number or
    # unrelated text.  Numeric-to-stroke fallback is only sound after the page
    # profile has explicitly been identified as numbered notation.
    visible_number = _visible_numeric_symbol(candidate) if is_numbered_profile else None
    if visible_number == "0":
        return None, "數字譜 0 是休止，不建立敲擊事件"
    instrument_id = str(data.get("instrument_id") or "")
    stroke_id = str(data.get("stroke_id") or "")
    if instrument_id in {"", UNKNOWN_ID}:
        return None, "樂器尚未對應到樂器庫"
    instrument = get_instrument(library, instrument_id)
    if instrument is None:
        return None, f"找不到樂器 ID：{instrument_id}"
    if stroke_id in {"", UNKNOWN_ID}:
        stroke_id = _numbered_stroke_id(instrument, visible_number) or UNKNOWN_ID
    if stroke_id == UNKNOWN_ID:
        return None, f"「{instrument.get('name', instrument_id)}」的擊法尚未對應"
    stroke = get_stroke(library, instrument_id, stroke_id)
    if stroke is None:
        return None, f"擊法不屬於「{instrument.get('name', instrument_id)}」：{stroke_id}"

    bar = _integer(data.get("bar"))
    bar_count = int(segment.get("bars", 0) or 0)
    if bar is None or not 1 <= bar <= bar_count:
        return None, f"小節號必須介於 1 與 {bar_count}"
    in_bar_tick = _integer(data.get("tick"))
    if in_bar_tick is None or in_bar_tick < 0:
        return None, "缺少有效的拍點 tick"

    settings = ensure_bar_settings(dict(segment))
    setting = settings[bar - 1]
    start_tick, end_tick = bar_range_ticks(dict(segment), bar)
    bar_size = end_tick - start_tick
    if in_bar_tick >= bar_size:
        return None, f"第 {bar} 小節內的 tick 超出範圍"
    step = grid_ticks(setting["subdivision"])
    cell_count = grid_cell_count(setting["time_signature"], setting["subdivision"])
    cell = min(cell_count - 1, max(0, int(in_bar_tick / step + 0.5)))
    tick = start_tick + cell * step
    beat = (cell * step) / ticks_per_beat(setting["time_signature"]) + 1
    duration = _integer(data.get("duration"))
    if duration is not None and duration <= 0:
        duration = None
    return {
        "kind": "event",
        "candidate_key": _candidate_key(run, candidate, candidate_index),
        "run_id": str(run.get("run_id") or ""),
        "candidate_id": str(candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"),
        "instrument_id": instrument_id,
        "instrument_name": str(instrument.get("name") or instrument_id),
        "sound_profile": str(instrument.get("sound_profile") or "bell"),
        "stroke_id": stroke_id,
        "stroke_code": str(stroke.get("code") or stroke_id),
        "stroke_name": str(stroke.get("name") or stroke.get("code") or stroke_id),
        "bar": bar,
        "beat": round(beat, 6),
        "tick": tick,
        "raw_tick": start_tick + in_bar_tick,
        "grid_index": cell,
        "quantization": setting["subdivision"],
        "duration": duration,
        "is_accent": bool(data.get("is_accent", False)),
        "is_ornament": bool(data.get("is_ornament", False)),
        "is_roll": bool(data.get("is_roll", False)),
        "source_evidence": deepcopy(candidate.get("source_evidence")),
    }, None


def _repeat_operation(
    segment: Mapping[str, object],
    run: Mapping[str, object],
    candidate: Mapping[str, object],
    candidate_index: int,
) -> tuple[dict | None, str | None]:
    data = _candidate_data(candidate)
    marker_type = str(data.get("type") or "")
    bar_count = int(segment.get("bars", 0) or 0)
    marker: dict[str, object] = {
        "type": marker_type,
        "source_candidate_key": _candidate_key(run, candidate, candidate_index),
        "source_run_id": str(run.get("run_id") or ""),
        "source_candidate_id": str(candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"),
    }
    if marker_type in {"repeat_start", "repeat_end"}:
        bar = _integer(data.get("bar"))
        if bar is None or not 1 <= bar <= bar_count:
            return None, f"{marker_type} 缺少有效小節號"
        marker["bar"] = bar
        if marker_type == "repeat_end":
            times = _integer(data.get("times")) or 2
            if not 2 <= times <= 32:
                return None, "反覆次數必須介於 2 與 32"
            marker["times"] = times
        return {"kind": "repeat", "marker": marker, "candidate_key": marker["source_candidate_key"]}, None
    if marker_type == "volta":
        start = _integer(data.get("start_bar"))
        end = _integer(data.get("end_bar"))
        passes = data.get("passes")
        if (
            start is None
            or end is None
            or not 1 <= start <= end <= bar_count
            or not isinstance(passes, list)
            or not passes
            or any(_integer(value) is None or int(value) <= 0 for value in passes)
        ):
            return None, "第一／第二結尾資料不完整"
        marker.update({
            "start_bar": start,
            "end_bar": end,
            "passes": sorted(set(int(value) for value in passes)),
        })
        return {"kind": "repeat", "marker": marker, "candidate_key": marker["source_candidate_key"]}, None
    return None, "反覆記號類型無法套用"


def build_application_plan(
    segment: Mapping[str, object],
    recognition_runs: Iterable[Mapping[str, object]],
    library: Mapping[str, object],
) -> dict:
    """Return a serializable preview of reviewed candidates for one Segment."""

    segment_id = str(segment.get("segment_id") or "")
    existing_keys = _existing_candidate_keys(segment)
    operations: list[dict] = []
    skipped: list[dict] = []
    seen = set(existing_keys)
    for run in recognition_runs:
        if str(run.get("segment_id") or "") != segment_id:
            continue
        # A source crop can be corrected after a run finishes.  Keep the old
        # candidates as audit history, but never offer them for materializing
        # once that run is explicitly stale/failed/superseded.
        if run.get("status") in {"stale", "failed", "superseded"}:
            continue
        for index, candidate in enumerate(run.get("candidates", [])):
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("review_status") not in REVIEWED_STATUSES:
                continue
            key = _candidate_key(run, candidate, index)
            if key in seen:
                skipped.append({"candidate_key": key, "reason": "已經套用過"})
                continue
            kind = str(candidate.get("kind") or "")
            if kind == "event":
                operation, reason = _event_operation(
                    segment, run, candidate, index, library
                )
            elif kind == "repeat":
                operation, reason = _repeat_operation(segment, run, candidate, index)
            else:
                operation, reason = None, f"{kind or 'unknown'} 候選不會寫入節奏格"
            if operation is None:
                skipped.append({"candidate_key": key, "reason": reason or "無法套用"})
                continue
            seen.add(key)
            operations.append(operation)

    event_count = sum(item.get("kind") == "event" for item in operations)
    repeat_count = sum(item.get("kind") == "repeat" for item in operations)
    instruments = sorted({
        str(item.get("instrument_name"))
        for item in operations
        if item.get("kind") == "event"
    })
    return {
        "segment_id": segment_id,
        "operations": operations,
        "event_count": event_count,
        "repeat_count": repeat_count,
        "instruments": instruments,
        "skipped": skipped,
        "already_applied_count": sum(
            item.get("reason") == "已經套用過" for item in skipped
        ),
    }


def _new_track(segment: dict, instrument: Mapping[str, object]) -> dict:
    tracks = segment.setdefault("tracks", [])
    segment_id = str(segment["segment_id"])
    used_track_ids = {
        str(track.get("track_id"))
        for track in tracks
        if isinstance(track, Mapping) and track.get("track_id")
    }
    track_number = 1
    while f"{segment_id}-T{track_number:02d}" in used_track_ids:
        track_number += 1
    existing_orders = [
        parsed
        for track in tracks
        if isinstance(track, Mapping)
        for parsed in [_integer(track.get("track_order"))]
        if parsed is not None
    ]
    order = max(existing_orders, default=0) + 1
    brushes = [
        {
            "code": str(stroke.get("code") or f"stroke_{index + 1}"),
            "label": str(stroke.get("name") or stroke.get("code") or f"擊法 {index + 1}"),
            "color": str(stroke.get("color") or BRUSH_COLORS[index % len(BRUSH_COLORS)]),
            "stroke_id": stroke.get("stroke_id"),
        }
        for index, stroke in enumerate(instrument.get("strokes", []))
        if isinstance(stroke, Mapping)
    ]
    if not brushes:
        raise CandidateApplicationError(f"樂器「{instrument.get('name')}」沒有可用擊法")
    track = {
        "track_id": f"{segment_id}-T{track_number:02d}",
        "segment_id": segment_id,
        "instrument_id": instrument["instrument_id"],
        "instrument": str(instrument.get("name") or instrument["instrument_id"]),
        "sound_profile": str(instrument.get("sound_profile") or "bell"),
        "track_order": order,
        "is_complete": False,
        "selected_take_id": None,
        "notes": "由 AI 辨識候選建立；仍需人工校對。",
        "keymap": dict(zip(("f", "g", "h", "j", "k"), [item["code"] for item in brushes[:5]])),
        "brushes": brushes,
        "takes": [],
    }
    tracks.append(track)
    return track


def _ensure_track_stroke_brush(
    track: dict,
    stroke: Mapping[str, object],
) -> str | None:
    """Add one missing library stroke without rewriting existing brushes.

    Older projects can have a Track created before a newly defined stroke
    (for example ``pitch_6``) existed.  The grid resolves an Event's label and
    color through ``track.brushes``, so append the exact library record when it
    is missing.  A legacy brush with the same code but no stable ID is linked
    in place instead of duplicated.
    """

    stroke_id = str(stroke.get("stroke_id") or "")
    code = str(stroke.get("code") or "")
    if not stroke_id or not code:
        return "樂器庫擊法缺少穩定 ID 或代碼"

    raw_brushes = track.get("brushes")
    if raw_brushes is None:
        brushes: list[object] = []
        track["brushes"] = brushes
    elif isinstance(raw_brushes, list):
        brushes = raw_brushes
    else:
        return f"軌道「{track.get('instrument', '')}」的擊法資料格式無效"

    for brush in brushes:
        if isinstance(brush, Mapping) and brush.get("stroke_id") == stroke_id:
            return None

    same_code = [
        brush
        for brush in brushes
        if isinstance(brush, dict) and str(brush.get("code") or "") == code
    ]
    if len(same_code) == 1 and same_code[0].get("stroke_id") in {
        None,
        "",
        UNKNOWN_ID,
    }:
        # Preserve the user's label/color and only supply the stable link.
        same_code[0]["stroke_id"] = stroke_id
        return None
    if same_code:
        return f"軌道已有代碼「{code}」但對應到不同擊法，請先人工整理"
    if len(brushes) >= 9:
        return "目前軌道已有 9 種擊法，無法再加入新的擊法筆刷"

    brushes.append(
        {
            "code": code,
            "label": str(stroke.get("name") or code),
            "color": str(
                stroke.get("color")
                or BRUSH_COLORS[len(brushes) % len(BRUSH_COLORS)]
            ),
            "stroke_id": stroke_id,
        }
    )
    return None


def _ai_take(track: dict) -> dict:
    for take in track.get("takes", []):
        if isinstance(take, dict) and take.get("input_method") == AI_INPUT_METHOD:
            return take
    take_id = next_take_id(track)
    take = {
        "take_id": take_id,
        "created_at": _now_iso(),
        "quantization": "per-bar-grid",
        "count_in_bars": 0,
        "output_latency_ms": 0,
        "input_method": AI_INPUT_METHOD,
        "review_status": "needs_review",
        "events": [],
    }
    track.setdefault("takes", []).append(take)
    return take


def apply_application_plan(
    project: dict,
    segment: dict,
    plan: Mapping[str, object],
    library: Mapping[str, object],
) -> dict:
    """Apply a previously previewed plan without overwriting human takes."""

    if plan.get("segment_id") != segment.get("segment_id"):
        raise CandidateApplicationError("套用預覽不屬於目前段落")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise CandidateApplicationError("套用預覽格式無效")

    created_tracks = 0
    applied_events = 0
    applied_repeats = 0
    conflicts: list[str] = []
    instrument_ids = [
        str(track.get("instrument_id"))
        for track in segment.get("tracks", [])
        if isinstance(track, dict) and track.get("instrument_id")
    ]
    ambiguous_instrument_ids = {
        instrument_id
        for instrument_id in instrument_ids
        if instrument_ids.count(instrument_id) > 1
    }
    track_cache: dict[str, dict] = {}
    for instrument_id in set(instrument_ids):
        track = _track_for_instrument(segment, instrument_id)
        if isinstance(track, dict):
            track_cache[instrument_id] = track
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        if operation.get("kind") == "event":
            instrument_id = str(operation.get("instrument_id") or "")
            if instrument_id in ambiguous_instrument_ids:
                conflicts.append(
                    f"樂器「{operation.get('instrument_name') or instrument_id}」對應到多個 Track，"
                    "請先指定要寫入的軌道"
                )
                continue
            track = track_cache.get(instrument_id)
            if track is None:
                instrument = get_instrument(library, instrument_id)
                if instrument is None:
                    conflicts.append(f"找不到樂器 ID：{instrument_id}")
                    continue
                track = _new_track(segment, instrument)
                track_cache[instrument_id] = track
                created_tracks += 1
            instrument = get_instrument(library, instrument_id)
            stroke_id = str(operation.get("stroke_id") or "")
            stroke = get_stroke(library, instrument_id, stroke_id)
            if instrument is None or stroke is None:
                conflicts.append(
                    f"樂器「{operation.get('instrument_name') or instrument_id}」"
                    f"找不到擊法 ID：{stroke_id or '—'}"
                )
                continue
            take = _ai_take(track)
            tick = int(operation["tick"])
            occupied = next(
                (event for event in take.get("events", []) if int(event.get("tick", -1)) == tick),
                None,
            )
            if occupied is not None:
                conflicts.append(
                    f"{track.get('instrument')} 第 {operation.get('bar')} 小節有兩個候選落在同一格"
                )
                continue
            brush_error = _ensure_track_stroke_brush(track, stroke)
            if brush_error is not None:
                conflicts.append(brush_error)
                continue
            event_id = next_event_id(project)
            event = {
                "event_id": event_id,
                "segment_id": segment["segment_id"],
                "track_id": track["track_id"],
                "take_id": take["take_id"],
                "bar": operation["bar"],
                "beat": operation["beat"],
                "tick": tick,
                "raw_tick": operation["raw_tick"],
                "onset_ms": None,
                "stroke": operation["stroke_code"],
                "stroke_id": operation["stroke_id"],
                "is_accent": bool(operation.get("is_accent")),
                "is_ornament": bool(operation.get("is_ornament")),
                "is_roll": bool(operation.get("is_roll")),
                "duration": operation.get("duration"),
                "velocity": 100,
                "key_code": None,
                "input_group": "ai",
                "quantization": operation["quantization"],
                "grid_index": operation["grid_index"],
                "input_method": AI_INPUT_METHOD,
                "source_run_id": operation["run_id"],
                "source_candidate_id": operation["candidate_id"],
                "source_candidate_key": operation["candidate_key"],
                "source_evidence": deepcopy(operation.get("source_evidence")),
            }
            take.setdefault("events", []).append(event)
            take["events"].sort(key=lambda item: int(item.get("tick", 0)))
            track["selected_take_id"] = take["take_id"]
            applied_events += 1
        elif operation.get("kind") == "repeat":
            marker = operation.get("marker")
            if not isinstance(marker, Mapping):
                continue
            navigation = segment.setdefault("navigation", {})
            markers = navigation.setdefault("markers", [])
            markers.append(deepcopy(dict(marker)))
            navigation["playback_order"] = []
            navigation["review_status"] = "needs_review"
            applied_repeats += 1

    segment["updated_at"] = _now_iso()
    if applied_events:
        # Keep the project-level catalog aligned with the IDs just written.
        # Otherwise reopening against an older snapshot can downgrade the new
        # Track brush/Event stroke back to ``unknown`` during library binding.
        attach_project_snapshot(project, library)
    return {
        "applied_events": applied_events,
        "applied_repeats": applied_repeats,
        "created_tracks": created_tracks,
        "conflicts": conflicts,
    }


__all__ = [
    "AI_INPUT_METHOD",
    "CandidateApplicationError",
    "apply_application_plan",
    "build_application_plan",
]
