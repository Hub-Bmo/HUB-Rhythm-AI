"""Turn a confirmed whole-score layout plan into formal Segments.

This module deliberately contains no Tk or image-processing code.  It joins
three already-reviewed pieces of data:

* the confirmed page order (``page_sequence``),
* one :mod:`score_layout` analysis per page, and
* the user's final segment proposals.

The public helpers are non-mutating.  A caller may therefore build a preview,
discard it, or save the returned project without risking the project currently
open in the editor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from numbers import Integral, Real

from .models import new_segment, next_segment_id
from .score_plan import (
    ScorePlanError,
    new_source_ref,
    suggest_segment_ranges,
    with_segment_source_refs,
)


class SegmentPlanApplyError(ScorePlanError):
    """A confirmed page/segment plan cannot be applied without guessing."""


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise SegmentPlanApplyError(f"{field_name} must be a positive integer")
    if isinstance(value, Integral):
        result = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise SegmentPlanApplyError(f"{field_name} must be a positive integer")
    if result <= 0:
        raise SegmentPlanApplyError(f"{field_name} must be a positive integer")
    return result


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SegmentPlanApplyError(f"{field_name} cannot be empty")
    return value.strip()


def _bbox(value: object, field_name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise SegmentPlanApplyError(f"{field_name} must be an object")
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        coordinate = value.get(key)
        if isinstance(coordinate, bool) or not isinstance(coordinate, Real):
            raise SegmentPlanApplyError(
                f"{field_name} must contain numeric x, y, width, and height"
            )
        result[key] = float(coordinate)
    if result["x"] < 0 or result["y"] < 0:
        raise SegmentPlanApplyError(f"{field_name} starts outside the image")
    if result["width"] <= 0 or result["height"] <= 0:
        raise SegmentPlanApplyError(f"{field_name} must have positive size")
    tolerance = 1e-9
    if result["x"] + result["width"] > 1.0 + tolerance:
        raise SegmentPlanApplyError(f"{field_name} exceeds the image width")
    if result["y"] + result["height"] > 1.0 + tolerance:
        raise SegmentPlanApplyError(f"{field_name} exceeds the image height")
    return result


def _page_ids(page_sequence: Iterable[object]) -> list[str]:
    """Normalize either an ordered ID list or asset records with sequence values."""

    records = list(page_sequence)
    if not records:
        raise SegmentPlanApplyError("page_sequence cannot be empty")

    if all(isinstance(item, str) for item in records):
        asset_ids = [_required_text(item, "page_sequence asset_id") for item in records]
    elif all(isinstance(item, Mapping) for item in records):
        sequenced: list[tuple[int, int, str]] = []
        used_sequences: set[int] = set()
        for index, item in enumerate(records):
            assert isinstance(item, Mapping)
            asset_id = _required_text(item.get("asset_id"), "asset.asset_id")
            sequence = _positive_integer(
                item.get("page_sequence"), f"{asset_id}.page_sequence"
            )
            if sequence in used_sequences:
                raise SegmentPlanApplyError(
                    f"page_sequence contains duplicate number {sequence}"
                )
            used_sequences.add(sequence)
            sequenced.append((sequence, index, asset_id))
        sequenced.sort()
        asset_ids = [asset_id for _sequence, _index, asset_id in sequenced]
    else:
        raise SegmentPlanApplyError(
            "page_sequence must contain only asset IDs or only asset records"
        )

    if len(asset_ids) != len(set(asset_ids)):
        raise SegmentPlanApplyError("page_sequence contains a duplicate asset_id")
    return asset_ids


def flatten_measure_sequence(
    analyses_by_asset: Mapping[str, Mapping[str, object]],
    page_sequence: Iterable[object],
    *,
    start_global_bar: int = 1,
) -> list[dict[str, object]]:
    """Flatten reviewed page analyses into one cross-page measure sequence.

    Pages follow ``page_sequence``.  Systems and measures are sorted by their
    normalized image positions, so the output is stable even if a UI retained
    the analysis objects in a different list order.
    """

    first_bar = _positive_integer(start_global_bar, "start_global_bar")
    page_ids = _page_ids(page_sequence)
    unknown_analyses = sorted(set(analyses_by_asset) - set(page_ids))
    if unknown_analyses:
        raise SegmentPlanApplyError(
            "analyses_by_asset contains pages outside page_sequence: "
            + ", ".join(unknown_analyses)
        )

    measures: list[dict[str, object]] = []
    next_global_bar = first_bar
    for page_number, asset_id in enumerate(page_ids, start=1):
        analysis = analyses_by_asset.get(asset_id)
        if not isinstance(analysis, Mapping):
            raise SegmentPlanApplyError(f"missing layout analysis for {asset_id}")
        systems_value = analysis.get("systems")
        if not isinstance(systems_value, Sequence) or isinstance(
            systems_value, (str, bytes)
        ):
            raise SegmentPlanApplyError(f"{asset_id}.systems must be a list")

        systems: list[tuple[float, float, int, Mapping[str, object], dict[str, float]]] = []
        seen_system_ids: set[str] = set()
        for system_index, system in enumerate(systems_value):
            if not isinstance(system, Mapping):
                raise SegmentPlanApplyError(f"{asset_id}.systems contains an invalid item")
            system_id = _required_text(
                system.get("system_id"), f"{asset_id}.systems[{system_index}].system_id"
            )
            if system_id in seen_system_ids:
                raise SegmentPlanApplyError(
                    f"{asset_id} contains duplicate system_id {system_id}"
                )
            seen_system_ids.add(system_id)
            system_bbox = _bbox(
                system.get("bbox"), f"{asset_id}.{system_id}.bbox"
            )
            systems.append(
                (system_bbox["y"], system_bbox["x"], system_index, system, system_bbox)
            )
        systems.sort(key=lambda item: (item[0], item[1], item[2]))

        page_measure_count = 0
        for _y, _x, _system_index, system, system_bbox in systems:
            system_id = str(system["system_id"]).strip()
            regions_value = system.get("measure_regions")
            if not isinstance(regions_value, Sequence) or isinstance(
                regions_value, (str, bytes)
            ):
                raise SegmentPlanApplyError(
                    f"{asset_id}.{system_id}.measure_regions must be a list"
                )
            regions: list[tuple[float, float, int, Mapping[str, object], dict[str, float]]] = []
            for measure_index, region in enumerate(regions_value):
                if not isinstance(region, Mapping):
                    raise SegmentPlanApplyError(
                        f"{asset_id}.{system_id}.measure_regions contains an invalid item"
                    )
                measure_bbox = _bbox(
                    region.get("bbox"),
                    f"{asset_id}.{system_id}.measure_regions[{measure_index}].bbox",
                )
                regions.append(
                    (
                        measure_bbox["x"],
                        measure_bbox["y"],
                        measure_index,
                        region,
                        measure_bbox,
                    )
                )
            regions.sort(key=lambda item: (item[0], item[1], item[2]))
            for _measure_x, _measure_y, _measure_index, region, measure_bbox in regions:
                page_measure_count += 1
                measure_id = region.get("measure_id")
                origin = region.get("origin")
                if not isinstance(origin, str) or not origin.strip():
                    origin = system.get("origin")
                if not isinstance(origin, str) or not origin.strip():
                    origin = "detected"
                record: dict[str, object] = {
                    "asset_id": asset_id,
                    "system_id": system_id,
                    "bbox": measure_bbox,
                    "system_bbox": deepcopy(system_bbox),
                    "origin": origin.strip(),
                    "global_bar": next_global_bar,
                    "page_sequence": page_number,
                    "page_measure": page_measure_count,
                }
                if isinstance(measure_id, str) and measure_id.strip():
                    record["measure_id"] = measure_id.strip()
                confidence = region.get("confidence")
                if (
                    isinstance(confidence, Real)
                    and not isinstance(confidence, bool)
                    and 0 <= float(confidence) <= 1
                ):
                    record["confidence"] = float(confidence)
                measures.append(record)
                next_global_bar += 1

        if page_measure_count == 0:
            raise SegmentPlanApplyError(
                f"{asset_id} has no confirmed measure regions; refusing a partial score"
            )

    return measures


def _union_bbox(measures: Sequence[Mapping[str, object]]) -> dict[str, float]:
    boxes = [_bbox(measure.get("bbox"), "measure.bbox") for measure in measures]
    left = min(box["x"] for box in boxes)
    top = min(box["y"] for box in boxes)
    right = max(box["x"] + box["width"] for box in boxes)
    bottom = max(box["y"] + box["height"] for box in boxes)
    return {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }


def _proposal_ranges(
    measure_sequence: Sequence[Mapping[str, object]],
    proposals: Iterable[Mapping[str, object]] | None,
    *,
    target_bars: int,
) -> list[dict[str, object]]:
    first_bar = int(measure_sequence[0]["global_bar"])
    last_bar = int(measure_sequence[-1]["global_bar"])
    if proposals is None:
        return suggest_segment_ranges(first_bar, last_bar, target_bars=target_bars)

    result: list[dict[str, object]] = []
    expected_start = first_bar
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            raise SegmentPlanApplyError(f"proposal {index + 1} must be an object")
        start = _positive_integer(
            proposal.get("start_bar", proposal.get("start_global_bar")),
            f"proposal {index + 1}.start_bar",
        )
        end = _positive_integer(
            proposal.get("end_bar", proposal.get("end_global_bar")),
            f"proposal {index + 1}.end_bar",
        )
        if end < start:
            raise SegmentPlanApplyError(
                f"proposal {index + 1} ends before it starts"
            )
        if start != expected_start:
            raise SegmentPlanApplyError(
                f"segment proposals must cover every measure once; expected bar "
                f"{expected_start}, got {start}"
            )
        normalized = deepcopy(dict(proposal))
        normalized["start_bar"] = start
        normalized["end_bar"] = end
        normalized["bar_count"] = end - start + 1
        result.append(normalized)
        expected_start = end + 1
    if not result:
        raise SegmentPlanApplyError("segment proposals cannot be empty")
    if expected_start != last_bar + 1:
        raise SegmentPlanApplyError(
            f"segment proposals stop at bar {expected_start - 1}; score ends at {last_bar}"
        )
    return result


def _is_pristine_default_segment(segment: object) -> bool:
    """Return True for an untouched placeholder even after normal migration.

    App startup adds stable instrument/stroke IDs to the default tracks, so a
    byte-for-byte comparison with :func:`new_segment` would leave a confusing
    extra empty BG0001 in every newly planned score.
    """
    if not isinstance(segment, Mapping):
        return False
    segment_id = segment.get("segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        return False

    # Compare the complete editable structure instead of merely checking for
    # events.  An empty Segment may still contain valuable work such as a BPM,
    # mixed meter, custom brushes, renamed Tracks, or an intentionally-created
    # empty Take.  The only differences introduced automatically at startup are
    # timestamps plus stable instrument/stroke IDs, so only those fields are
    # ignored here.
    def comparable(value: Mapping[str, object]) -> dict[str, object]:
        result = deepcopy(dict(value))
        result.pop("created_at", None)
        result.pop("updated_at", None)
        tracks = result.get("tracks")
        if isinstance(tracks, list):
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                track.pop("instrument_id", None)
                brushes = track.get("brushes")
                if isinstance(brushes, list):
                    for brush in brushes:
                        if isinstance(brush, dict):
                            brush.pop("stroke_id", None)
        return result

    return comparable(segment) == comparable(new_segment(segment_id))


def _segment_source_refs(
    measures: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    groups: list[list[Mapping[str, object]]] = []
    for measure in measures:
        if (
            groups
            and groups[-1][-1].get("asset_id") == measure.get("asset_id")
            and groups[-1][-1].get("system_id") == measure.get("system_id")
            and groups[-1][-1].get("origin", "detected")
            == measure.get("origin", "detected")
            and int(measure["global_bar"]) == int(groups[-1][-1]["global_bar"]) + 1
        ):
            groups[-1].append(measure)
        else:
            groups.append([measure])

    segment_global_start = int(measures[0]["global_bar"])
    refs: list[dict[str, object]] = []
    for sequence, group in enumerate(groups, start=1):
        source_start = int(group[0]["global_bar"])
        source_end = int(group[-1]["global_bar"])
        segment_start = source_start - segment_global_start + 1
        segment_end = source_end - segment_global_start + 1
        source_ref = new_source_ref(
            str(group[0]["asset_id"]),
            _union_bbox(group),
            sequence=sequence,
            source_start_bar=source_start,
            source_end_bar=source_end,
            segment_start_bar=segment_start,
            segment_end_bar=segment_end,
            system_id=str(group[0]["system_id"]),
            origin=str(group[0].get("origin") or "detected"),
            review_status="confirmed",
        )
        # Keep the main crop limited to the selected measures.  A separate
        # system context crop preserves the left-hand instrument labels without
        # changing the candidate coordinate space of the main crop.
        context_bbox = group[0].get("system_bbox")
        if isinstance(context_bbox, Mapping):
            source_ref["context_bbox"] = _bbox(
                context_bbox, "measure.system_bbox"
            )
        refs.append(source_ref)
    return refs


def _configure_segment(
    segment_id: str,
    measures: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    segment = new_segment(segment_id)
    start_bar = int(measures[0]["global_bar"])
    end_bar = int(measures[-1]["global_bar"])
    bar_count = end_bar - start_bar + 1
    segment["bars"] = bar_count
    segment["source_start_bar"] = start_bar
    segment["source_end_bar"] = end_bar
    segment["bar_settings"] = [
        {
            "bar": index,
            "time_signature": segment["time_signature"],
            "subdivision": segment["subdivision"],
        }
        for index in range(1, bar_count + 1)
    ]
    return with_segment_source_refs(segment, _segment_source_refs(measures))


def apply_segment_proposals(
    project: Mapping[str, object],
    analyses_by_asset: Mapping[str, Mapping[str, object]],
    proposals: Iterable[Mapping[str, object]] | None = None,
    *,
    page_sequence: Iterable[object] | None = None,
    target_bars: int = 4,
    start_global_bar: int = 1,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a new project and summary for a confirmed whole-score plan.

    Existing Segments are preserved.  The initial completely untouched
    ``new_project()`` placeholder may be replaced by the first planned Segment;
    an edited Segment, even one with no takes yet, is never reused.
    """

    if not isinstance(project, Mapping):
        raise SegmentPlanApplyError("project must be an object")
    result: dict[str, object] = deepcopy(dict(project))
    assets = result.get("assets")
    if page_sequence is None:
        if not isinstance(assets, list):
            raise SegmentPlanApplyError("project.assets must be a list")
        confirmed_pages: Iterable[object] = assets
    else:
        confirmed_pages = page_sequence

    measure_sequence = flatten_measure_sequence(
        analyses_by_asset,
        confirmed_pages,
        start_global_bar=start_global_bar,
    )
    ranges = _proposal_ranges(
        measure_sequence,
        proposals,
        target_bars=target_bars,
    )
    measure_by_bar = {
        int(measure["global_bar"]): measure for measure in measure_sequence
    }

    existing_value = result.get("segments")
    if not isinstance(existing_value, list):
        raise SegmentPlanApplyError("project.segments must be a list")
    existing_segments = existing_value
    reusable_index = next(
        (
            index
            for index, segment in enumerate(existing_segments)
            if _is_pristine_default_segment(segment)
        ),
        None,
    )

    # Allocate against all original IDs (including the reusable placeholder),
    # then append each allocation so next_segment_id cannot repeat an ID.
    allocator = {"segments": deepcopy(existing_segments)}
    planned_segments: list[dict[str, object]] = []
    reused_segment_id: str | None = None
    for range_index, proposal in enumerate(ranges):
        if range_index == 0 and reusable_index is not None:
            reusable = existing_segments[reusable_index]
            assert isinstance(reusable, Mapping)
            segment_id = str(reusable["segment_id"])
            reused_segment_id = segment_id
        else:
            segment_id = next_segment_id(allocator)
            allocator["segments"].append({"segment_id": segment_id})
        start = int(proposal["start_bar"])
        end = int(proposal["end_bar"])
        selected_measures = [measure_by_bar[bar] for bar in range(start, end + 1)]
        planned_segments.append(_configure_segment(segment_id, selected_measures))

    if reusable_index is None:
        final_segments = existing_segments + planned_segments
    else:
        final_segments = (
            existing_segments[:reusable_index]
            + planned_segments
            + existing_segments[reusable_index + 1 :]
        )
    result["segments"] = final_segments

    preserved_ids = [
        str(segment.get("segment_id", ""))
        for index, segment in enumerate(existing_segments)
        if isinstance(segment, Mapping) and index != reusable_index
    ]
    summary: dict[str, object] = {
        "page_count": len(_page_ids(confirmed_pages)),
        "measure_count": len(measure_sequence),
        "measure_sequence": deepcopy(measure_sequence),
        "proposal_count": len(ranges),
        "proposals": deepcopy(ranges),
        "created_segment_ids": [
            str(segment["segment_id"]) for segment in planned_segments
        ],
        "created_source_ref_count": sum(
            len(segment.get("source_refs", [])) for segment in planned_segments
        ),
        "reused_segment_id": reused_segment_id,
        "preserved_segment_ids": preserved_ids,
    }
    return result, summary


__all__ = [
    "SegmentPlanApplyError",
    "apply_segment_proposals",
    "flatten_measure_sequence",
]
