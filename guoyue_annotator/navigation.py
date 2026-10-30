"""Pure score-navigation helpers for repeats and volta endings.

Events remain attached to their one canonical paper-score position.  This
module expands only a list of source-bar references for playback/tokenization,
so repeat playback never duplicates or mutates Event records.

Marker schema (bar numbers are one-based)::

    {"type": "repeat_start", "bar": 2}
    {"type": "repeat_end", "bar": 4, "times": 2}
    {"type": "volta", "start_bar": 4, "end_bar": 4, "passes": [1]}
    {"type": "volta", "start_bar": 5, "end_bar": 5, "passes": [2]}

For conventional first/second endings, the repeat-end symbol may sit at the
end of the first ending.  A contiguous second volta immediately after it is
treated as part of the same logical repeat section.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace


MAX_REPEAT_PASSES = 32


@dataclass(frozen=True)
class NavigationIssue:
    """A validation problem suitable for UI display or test assertions."""

    code: str
    message: str
    marker_index: int | None = None
    bar: int | None = None


class NavigationError(ValueError):
    """Raised when paper-score navigation cannot be expanded unambiguously."""

    def __init__(self, issues: Iterable[NavigationIssue]):
        self.issues = tuple(issues)
        self.errors = tuple(issue.message for issue in self.issues)
        super().__init__("；".join(self.errors) or "樂譜導航資料無效")


@dataclass(frozen=True)
class _RepeatMarker:
    kind: str
    bar: int
    marker_index: int
    times: int = 2


@dataclass(frozen=True)
class _Volta:
    start_bar: int
    end_bar: int
    passes: tuple[int, ...]
    marker_index: int
    repeat_start_bar: int | None = None


@dataclass(frozen=True)
class _RepeatSection:
    start_bar: int
    repeat_end_bar: int
    times: int
    logical_end_bar: int
    voltas: tuple[_Volta, ...] = ()


def _issue(
    code: str,
    message: str,
    *,
    marker_index: int | None = None,
    bar: int | None = None,
) -> NavigationIssue:
    return NavigationIssue(code, message, marker_index, bar)


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


def _marker_kind(marker: Mapping[str, object]) -> str:
    return str(marker.get("type", marker.get("kind", ""))).strip().lower()


def _parse_passes(marker: Mapping[str, object]) -> tuple[int, ...] | None:
    value = marker.get(
        "passes",
        marker.get("pass_numbers", marker.get("numbers", marker.get("number"))),
    )
    if isinstance(value, str):
        values: list[object] = [item.strip() for item in value.split(",")]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = list(value)
    else:
        values = [value]
    parsed = [_integer(item) for item in values]
    if not parsed or any(item is None or item <= 0 for item in parsed):
        return None
    return tuple(sorted(set(int(item) for item in parsed if item is not None)))


def _parse_markers(
    bar_count: int,
    markers: Iterable[Mapping[str, object]],
) -> tuple[list[_RepeatMarker], list[_Volta]]:
    issues: list[NavigationIssue] = []
    repeat_markers: list[_RepeatMarker] = []
    voltas: list[_Volta] = []

    try:
        marker_list = list(markers)
    except TypeError:
        raise NavigationError((
            _issue("invalid_markers", "navigation markers 必須是可迭代清單"),
        )) from None

    for index, marker in enumerate(marker_list):
        if not isinstance(marker, Mapping):
            issues.append(_issue(
                "invalid_marker",
                f"第 {index + 1} 個 navigation marker 不是物件",
                marker_index=index,
            ))
            continue
        kind = _marker_kind(marker)
        if kind in {"repeat_start", "repeat_end"}:
            bar = _integer(marker.get("bar"))
            if bar is None or not 1 <= bar <= bar_count:
                issues.append(_issue(
                    "bar_out_of_range",
                    f"{kind} 的小節號必須介於 1 與 {bar_count}",
                    marker_index=index,
                    bar=bar,
                ))
                continue
            times = 2
            if kind == "repeat_end":
                raw_times = marker.get(
                    "times",
                    marker.get("repeat_count", marker.get("count", 2)),
                )
                parsed_times = _integer(raw_times)
                if (
                    parsed_times is None
                    or parsed_times < 2
                    or parsed_times > MAX_REPEAT_PASSES
                ):
                    issues.append(_issue(
                        "invalid_repeat_count",
                        f"反覆次數必須介於 2 與 {MAX_REPEAT_PASSES}",
                        marker_index=index,
                        bar=bar,
                    ))
                    continue
                times = parsed_times
            repeat_markers.append(_RepeatMarker(kind, bar, index, times))
            continue

        if kind == "volta":
            start_bar = _integer(marker.get("start_bar", marker.get("bar")))
            end_bar = _integer(marker.get("end_bar", start_bar))
            passes = _parse_passes(marker)
            repeat_start_bar = _integer(marker.get("repeat_start_bar"))
            if (
                start_bar is None
                or end_bar is None
                or not 1 <= start_bar <= end_bar <= bar_count
            ):
                issues.append(_issue(
                    "invalid_volta_range",
                    f"volta 範圍必須介於小節 1 與 {bar_count}，且起點不可晚於終點",
                    marker_index=index,
                    bar=start_bar,
                ))
                continue
            if passes is None:
                issues.append(_issue(
                    "invalid_volta_passes",
                    "volta 必須指定正整數 passes，例如 [1] 或 [2]",
                    marker_index=index,
                    bar=start_bar,
                ))
                continue
            voltas.append(_Volta(
                start_bar,
                end_bar,
                passes,
                index,
                repeat_start_bar,
            ))
            continue

        issues.append(_issue(
            "unsupported_marker",
            f"目前不支援 navigation marker：{kind or '(空白)'}",
            marker_index=index,
        ))

    if issues:
        raise NavigationError(issues)
    return repeat_markers, voltas


def _pair_repeats(repeat_markers: list[_RepeatMarker]) -> list[_RepeatSection]:
    issues: list[NavigationIssue] = []
    sections: list[_RepeatSection] = []
    active: _RepeatMarker | None = None
    seen: set[tuple[str, int]] = set()

    for marker in sorted(
        repeat_markers,
        key=lambda item: (item.bar, 0 if item.kind == "repeat_start" else 1),
    ):
        identity = (marker.kind, marker.bar)
        if identity in seen:
            issues.append(_issue(
                "duplicate_repeat_marker",
                f"小節 {marker.bar} 有重複的 {marker.kind}",
                marker_index=marker.marker_index,
                bar=marker.bar,
            ))
            continue
        seen.add(identity)
        if marker.kind == "repeat_start":
            if active is not None:
                issues.append(_issue(
                    "nested_repeat",
                    f"小節 {marker.bar} 在小節 {active.bar} 開始的反覆區段內又開始反覆",
                    marker_index=marker.marker_index,
                    bar=marker.bar,
                ))
            else:
                active = marker
            continue

        if active is None:
            issues.append(_issue(
                "orphan_repeat_end",
                f"小節 {marker.bar} 的 repeat_end 找不到 repeat_start",
                marker_index=marker.marker_index,
                bar=marker.bar,
            ))
            continue
        sections.append(_RepeatSection(
            active.bar,
            marker.bar,
            marker.times,
            marker.bar,
        ))
        active = None

    if active is not None:
        issues.append(_issue(
            "unclosed_repeat_start",
            f"小節 {active.bar} 的 repeat_start 沒有 repeat_end",
            marker_index=active.marker_index,
            bar=active.bar,
        ))
    if issues:
        raise NavigationError(issues)
    return sections


def _attach_voltas(
    sections: list[_RepeatSection],
    voltas: list[_Volta],
) -> list[_RepeatSection]:
    if not voltas:
        return sections

    issues: list[NavigationIssue] = []
    ordered_voltas = sorted(voltas, key=lambda item: (item.start_bar, item.end_bar))
    for previous, current in zip(ordered_voltas, ordered_voltas[1:]):
        if current.start_bar <= previous.end_bar:
            issues.append(_issue(
                "overlapping_voltas",
                f"小節 {current.start_bar} 的 volta 與前一個 volta 重疊",
                marker_index=current.marker_index,
                bar=current.start_bar,
            ))

    assignments: dict[int, list[_Volta]] = {index: [] for index in range(len(sections))}
    assigned: set[int] = set()

    # Explicit association wins and is useful when adjacent repeats would make
    # a photographed score genuinely ambiguous.
    for volta in ordered_voltas:
        if volta.repeat_start_bar is None:
            continue
        matches = [
            index for index, section in enumerate(sections)
            if section.start_bar == volta.repeat_start_bar
        ]
        if len(matches) != 1:
            issues.append(_issue(
                "unknown_volta_repeat",
                f"volta 指定的 repeat_start_bar {volta.repeat_start_bar} 找不到唯一反覆區段",
                marker_index=volta.marker_index,
                bar=volta.start_bar,
            ))
            continue
        assignments[matches[0]].append(volta)
        assigned.add(volta.marker_index)

    # A volta physically inside a repeat pair associates unambiguously.
    for volta in ordered_voltas:
        if volta.marker_index in assigned:
            continue
        matches = [
            index for index, section in enumerate(sections)
            if section.start_bar <= volta.start_bar <= section.repeat_end_bar
        ]
        if len(matches) == 1:
            assignments[matches[0]].append(volta)
            assigned.add(volta.marker_index)

    # Conventional second endings immediately follow the first ending's
    # repeat-end bar. Extend each section through contiguous volta ranges.
    for section_index, section in enumerate(sections):
        logical_end = max(
            (volta.end_bar for volta in assignments[section_index]),
            default=section.repeat_end_bar,
        )
        while True:
            candidates = [
                volta for volta in ordered_voltas
                if volta.marker_index not in assigned
                and volta.start_bar == logical_end + 1
                and volta.repeat_start_bar is None
            ]
            if len(candidates) != 1:
                if len(candidates) > 1:
                    issues.append(_issue(
                        "ambiguous_volta",
                        f"小節 {logical_end + 1} 有多個可能的 volta 延伸",
                        bar=logical_end + 1,
                    ))
                break
            volta = candidates[0]
            assignments[section_index].append(volta)
            assigned.add(volta.marker_index)
            logical_end = volta.end_bar

    for volta in ordered_voltas:
        if volta.marker_index not in assigned:
            issues.append(_issue(
                "orphan_volta",
                f"小節 {volta.start_bar}-{volta.end_bar} 的 volta 無法對應到反覆區段",
                marker_index=volta.marker_index,
                bar=volta.start_bar,
            ))

    result: list[_RepeatSection] = []
    for section_index, section in enumerate(sections):
        attached = tuple(sorted(
            assignments[section_index],
            key=lambda item: (item.start_bar, item.end_bar),
        ))
        logical_end = max(
            (volta.end_bar for volta in attached),
            default=section.repeat_end_bar,
        )
        for volta in attached:
            if volta.start_bar < section.start_bar or volta.end_bar > logical_end:
                issues.append(_issue(
                    "volta_outside_repeat",
                    f"小節 {volta.start_bar}-{volta.end_bar} 的 volta 超出所屬反覆區段",
                    marker_index=volta.marker_index,
                    bar=volta.start_bar,
                ))
            invalid_passes = [number for number in volta.passes if number > section.times]
            if invalid_passes:
                issues.append(_issue(
                    "volta_pass_out_of_range",
                    f"volta passes {invalid_passes} 超過反覆次數 {section.times}",
                    marker_index=volta.marker_index,
                    bar=volta.start_bar,
                ))

        if attached:
            pass_owners: dict[int, list[_Volta]] = {
                pass_index: [] for pass_index in range(1, section.times + 1)
            }
            for volta in attached:
                for pass_index in volta.passes:
                    if pass_index in pass_owners:
                        pass_owners[pass_index].append(volta)
            missing = [number for number, owners in pass_owners.items() if not owners]
            duplicate = [number for number, owners in pass_owners.items() if len(owners) > 1]
            if missing:
                issues.append(_issue(
                    "incomplete_volta_passes",
                    f"反覆區段 {section.start_bar}-{logical_end} 缺少 passes {missing} 的結尾",
                    bar=section.start_bar,
                ))
            if duplicate:
                issues.append(_issue(
                    "ambiguous_volta_passes",
                    f"反覆區段 {section.start_bar}-{logical_end} 的 passes {duplicate} 同時對應多個結尾",
                    bar=section.start_bar,
                ))

        result.append(replace(
            section,
            logical_end_bar=logical_end,
            voltas=attached,
        ))

    for previous, current in zip(result, result[1:]):
        if previous.logical_end_bar >= current.start_bar:
            issues.append(_issue(
                "overlapping_repeat_sections",
                f"反覆區段 {previous.start_bar}-{previous.logical_end_bar} 與 {current.start_bar}-{current.logical_end_bar} 重疊",
                bar=current.start_bar,
            ))

    if issues:
        raise NavigationError(issues)
    return result


def _compile_navigation(
    bar_count: int,
    markers: Iterable[Mapping[str, object]],
) -> list[_RepeatSection]:
    parsed_bar_count = _integer(bar_count)
    if parsed_bar_count is None or parsed_bar_count <= 0:
        raise NavigationError((
            _issue("invalid_bar_count", "bar_count 必須是正整數"),
        ))
    repeat_markers, voltas = _parse_markers(parsed_bar_count, markers)
    sections = _pair_repeats(repeat_markers)
    if voltas and not sections:
        raise NavigationError(tuple(
            _issue(
                "orphan_volta",
                f"小節 {volta.start_bar}-{volta.end_bar} 的 volta 沒有反覆區段",
                marker_index=volta.marker_index,
                bar=volta.start_bar,
            )
            for volta in voltas
        ))
    return _attach_voltas(sections, voltas)


def validate_navigation(
    bar_count: int,
    markers: Iterable[Mapping[str, object]],
) -> tuple[NavigationIssue, ...]:
    """Return structural issues without raising or mutating marker records."""
    try:
        _compile_navigation(bar_count, markers)
    except NavigationError as exc:
        return exc.issues
    return ()


def expand_playback_order(
    bar_count: int,
    markers: Iterable[Mapping[str, object]],
) -> list[dict[str, int]]:
    """Expand paper bars to playback references without copying Events.

    ``pass_index`` is local to the active repeat. Bars outside repeats use 1.
    The returned dictionaries are JSON-ready and intentionally contain no
    event payload.
    """
    sections = _compile_navigation(bar_count, markers)
    playback_order: list[dict[str, int]] = []
    cursor = 1

    def append_bar(source_bar: int, pass_index: int = 1) -> None:
        playback_order.append({
            "source_bar": source_bar,
            "pass_index": pass_index,
        })

    for section in sections:
        for source_bar in range(cursor, section.start_bar):
            append_bar(source_bar)

        for pass_index in range(1, section.times + 1):
            for source_bar in range(
                section.start_bar,
                section.logical_end_bar + 1,
            ):
                containing_volta = next((
                    volta for volta in section.voltas
                    if volta.start_bar <= source_bar <= volta.end_bar
                ), None)
                if (
                    containing_volta is not None
                    and pass_index not in containing_volta.passes
                ):
                    continue
                append_bar(source_bar, pass_index)

        cursor = section.logical_end_bar + 1

    for source_bar in range(cursor, int(bar_count) + 1):
        append_bar(source_bar)
    return playback_order


def _validated_playback_order(
    bar_count: int,
    playback_order: Iterable[Mapping[str, object]],
) -> list[tuple[int, int]]:
    try:
        entries = list(playback_order)
    except TypeError:
        raise NavigationError((
            _issue("invalid_playback_order", "playback_order 必須是可迭代清單"),
        )) from None
    if not entries:
        raise NavigationError((
            _issue("empty_playback_order", "playback_order 不可為空"),
        ))

    issues: list[NavigationIssue] = []
    parsed: list[tuple[int, int]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            issues.append(_issue(
                "invalid_playback_step",
                f"playback_order 第 {index + 1} 項不是物件",
                marker_index=index,
            ))
            continue
        source_bar = _integer(entry.get("source_bar"))
        pass_index = _integer(entry.get("pass_index", 1))
        if source_bar is None or not 1 <= source_bar <= bar_count:
            issues.append(_issue(
                "playback_source_bar_out_of_range",
                f"playback_order 第 {index + 1} 項的 source_bar 必須介於 1 與 {bar_count}",
                marker_index=index,
                bar=source_bar,
            ))
            continue
        if pass_index is None or pass_index <= 0:
            issues.append(_issue(
                "invalid_pass_index",
                f"playback_order 第 {index + 1} 項的 pass_index 必須是正整數",
                marker_index=index,
                bar=source_bar,
            ))
            continue
        parsed.append((source_bar, pass_index))
    if issues:
        raise NavigationError(issues)
    return parsed


def materialize_playback_view(
    segment: Mapping[str, object],
    tracks: Iterable[Mapping[str, object]],
    playback_order: Iterable[Mapping[str, object]],
) -> tuple[dict, list[dict]]:
    """Build a transient expanded view accepted by ``render_segment_wav``.

    The returned segment and tracks are deep copies. Events are copied once per
    source-bar occurrence, while their canonical ``event_id`` remains intact.
    ``source_bar``, ``source_tick`` and ``pass_index`` record where every
    temporary event came from. Variable meters work because each source bar's
    full bar setting is copied into the expanded timeline.
    """
    if not isinstance(segment, Mapping):
        raise NavigationError((
            _issue("invalid_segment", "segment 必須是物件"),
        ))

    # Grid normalization is intentionally applied only to a deep copy. Some
    # legacy projects do not yet have bar_settings, and ensure_bar_settings()
    # fills them in place.
    from .grid import bar_start_ticks, ensure_bar_settings
    from .timeline import ticks_per_bar, ticks_per_beat

    source_segment = deepcopy(dict(segment))
    source_settings = deepcopy(ensure_bar_settings(source_segment))
    source_bar_count = len(source_settings)
    order = _validated_playback_order(source_bar_count, playback_order)
    source_starts = bar_start_ticks(source_segment)
    source_total_ticks = sum(
        ticks_per_bar(setting["time_signature"])
        for setting in source_settings
    )

    expanded_settings: list[dict] = []
    current_tick = 0
    occurrences: dict[int, list[tuple[int, int, int]]] = {}
    json_order: list[dict[str, int]] = []
    for playback_bar, (source_bar, pass_index) in enumerate(order, start=1):
        source_setting = source_settings[source_bar - 1]
        setting = deepcopy(source_setting)
        setting["bar"] = playback_bar
        setting["source_bar"] = source_bar
        setting["pass_index"] = pass_index
        expanded_settings.append(setting)
        occurrences.setdefault(source_bar, []).append(
            (playback_bar, pass_index, current_tick)
        )
        json_order.append({
            "source_bar": source_bar,
            "pass_index": pass_index,
        })
        current_tick += ticks_per_bar(source_setting["time_signature"])

    try:
        track_list = list(tracks)
    except TypeError:
        raise NavigationError((
            _issue("invalid_tracks", "tracks 必須是可迭代清單"),
        )) from None
    if any(not isinstance(track, Mapping) for track in track_list):
        raise NavigationError((
            _issue("invalid_track", "tracks 中的每一項都必須是物件"),
        ))
    expanded_tracks: list[dict] = deepcopy([dict(track) for track in track_list])

    for track in expanded_tracks:
        for take in track.get("takes", []):
            source_events = list(take.get("events", []))
            expanded_events: list[dict] = []
            for event in source_events:
                if not isinstance(event, Mapping):
                    continue
                try:
                    source_tick = int(event.get("tick", 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if not 0 <= source_tick < source_total_ticks:
                    # Match the renderer's existing half-open segment range:
                    # malformed/out-of-range events are not audible.
                    continue
                source_bar_index = bisect_right(source_starts, source_tick) - 1
                source_bar = source_bar_index + 1
                relative_tick = source_tick - source_starts[source_bar_index]
                source_setting = source_settings[source_bar_index]
                beat_ticks = ticks_per_beat(source_setting["time_signature"])

                for playback_bar, pass_index, playback_start in occurrences.get(
                    source_bar, ()
                ):
                    copied_event = deepcopy(dict(event))
                    copied_event["source_bar"] = source_bar
                    copied_event["source_tick"] = source_tick
                    copied_event["pass_index"] = pass_index
                    copied_event["playback_bar"] = playback_bar
                    copied_event["bar"] = playback_bar
                    copied_event["tick"] = playback_start + relative_tick
                    copied_event["beat"] = round(relative_tick / beat_ticks + 1, 6)
                    expanded_events.append(copied_event)
            expanded_events.sort(key=lambda item: int(item.get("tick", 0)))
            take["events"] = expanded_events

    view_segment = deepcopy(source_segment)
    view_segment["bars"] = len(order)
    view_segment["bar_settings"] = expanded_settings
    view_segment["time_signature"] = expanded_settings[0]["time_signature"]
    view_segment["subdivision"] = expanded_settings[0]["subdivision"]
    navigation = view_segment.get("navigation")
    if not isinstance(navigation, dict):
        navigation = {}
        view_segment["navigation"] = navigation
    navigation["playback_order"] = json_order
    navigation["materialized_view"] = True
    view_segment["tracks"] = expanded_tracks
    return view_segment, expanded_tracks


__all__ = [
    "MAX_REPEAT_PASSES",
    "NavigationError",
    "NavigationIssue",
    "expand_playback_order",
    "materialize_playback_view",
    "validate_navigation",
]
