"""Pure helpers for planning a safe, sequential recognition batch.

The UI owns credentials and network execution.  This module only inspects a
project and decides which confirmed Segment source regions still need a run.
Keeping that decision free of Tk and provider code makes it deterministic and
easy to test before any request can incur cost.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from numbers import Real
from typing import Mapping


COMPLETED_REGION_STATUSES = frozenset(
    {"ready_for_review", "reviewed", "completed_empty", "completed", "applied"}
)
RETRYABLE_REGION_STATUSES = frozenset(
    {"failed", "stale", "interrupted", "pending"}
)


def summarize_batch_results(
    runs: object,
    run_ids: object,
) -> dict[str, list[str]]:
    """Classify one visible batch without making retry decisions implicit.

    The caller supplies the batch's saved run IDs so unrelated recognition
    history is never mixed into the summary.  A timeout is still a failed
    request, but it gets its own bucket so the UI can explain that a transient
    delay is different from a successful response with zero candidates.
    """

    summary = {
        "successful_run_ids": [],
        "completed_empty_run_ids": [],
        "timeout_run_ids": [],
        "failed_run_ids": [],
        "unfinished_run_ids": [],
        "retryable_run_ids": [],
    }
    run_lookup = {
        str(run.get("run_id")): run
        for run in runs
        if isinstance(run, Mapping)
        and isinstance(run.get("run_id"), str)
        and str(run.get("run_id")).strip()
    } if isinstance(runs, list) else {}
    ordered_ids = run_ids if isinstance(run_ids, list) else []
    seen: set[str] = set()
    for run_id_value in ordered_ids:
        if not isinstance(run_id_value, str) or not run_id_value.strip():
            continue
        run_id = run_id_value.strip()
        if run_id in seen:
            continue
        seen.add(run_id)
        run = run_lookup.get(run_id)
        if run is None:
            summary["unfinished_run_ids"].append(run_id)
            continue
        status = str(run.get("status") or "").strip().casefold()
        if status == "completed_empty":
            summary["completed_empty_run_ids"].append(run_id)
            continue
        if status in COMPLETED_REGION_STATUSES:
            summary["successful_run_ids"].append(run_id)
            continue
        if status in RETRYABLE_REGION_STATUSES:
            summary["retryable_run_ids"].append(run_id)
            error = run.get("error")
            if isinstance(error, Mapping):
                error_text = f"{error.get('type', '')} {error.get('message', '')}"
            else:
                error_text = str(error or "")
            normalized_error = error_text.casefold()
            if any(
                marker in normalized_error
                for marker in ("timeout", "timed out", "逾時", "超時")
            ):
                summary["timeout_run_ids"].append(run_id)
            elif status in {"failed", "stale"}:
                summary["failed_run_ids"].append(run_id)
            else:
                summary["unfinished_run_ids"].append(run_id)
            continue
        summary["unfinished_run_ids"].append(run_id)
    return summary


def _positive_sequence(value: object, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return fallback


def _valid_bbox(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        x = float(value["x"])
        y = float(value["y"])
        width = float(value["width"])
        height = float(value["height"])
    except (KeyError, TypeError, ValueError):
        return False
    if any(isinstance(value.get(key), bool) for key in ("x", "y", "width", "height")):
        return False
    if any(not isinstance(value.get(key), Real) for key in ("x", "y", "width", "height")):
        return False
    return (
        x >= 0
        and y >= 0
        and width > 0
        and height > 0
        and x + width <= 1 + 1e-9
        and y + height <= 1 + 1e-9
    )


def _canonical_intish(value: object) -> object:
    """Treat UI string numerals and stored integers as the same structure."""

    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def plan_recognition_batch(project: Mapping[str, object]) -> dict[str, object]:
    """Return confirmed source regions that do not already have a result.

    Jobs follow project Segment order and each Segment's explicit source-ref
    sequence.  A region is considered complete only for the same
    ``(segment_id, region_id)`` pair; the same photo crop may legitimately be
    used by another Segment with different musical context.
    """

    assets_value = project.get("assets", [])
    assets = {
        str(asset.get("asset_id")): asset
        for asset in assets_value
        if isinstance(asset, Mapping)
        and isinstance(asset.get("asset_id"), str)
        and str(asset.get("asset_id")).strip()
    } if isinstance(assets_value, list) else {}

    completed_runs: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    runs_value = project.get("recognition_runs", [])
    if isinstance(runs_value, list):
        for run in runs_value:
            if not isinstance(run, Mapping):
                continue
            segment_id = run.get("segment_id")
            region_id = run.get("region_id")
            if (
                isinstance(segment_id, str)
                and segment_id.strip()
                and isinstance(region_id, str)
                and region_id.strip()
                and run.get("status") in COMPLETED_REGION_STATUSES
            ):
                completed_runs.setdefault((segment_id, region_id), []).append(run)

    jobs: list[dict[str, object]] = []
    invalid: list[dict[str, object]] = []
    skipped_completed: list[dict[str, object]] = []
    stale_completed: list[dict[str, object]] = []
    skipped_unconfirmed: list[dict[str, object]] = []
    seen_pairs: set[tuple[str, str]] = set()
    segments_value = project.get("segments", [])
    segments = segments_value if isinstance(segments_value, list) else []
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            invalid.append({"segment_index": segment_index, "reason": "段落格式無效"})
            continue
        segment_id_value = segment.get("segment_id")
        segment_id = (
            segment_id_value.strip()
            if isinstance(segment_id_value, str) and segment_id_value.strip()
            else ""
        )
        refs_value = segment.get("source_refs", [])
        refs = refs_value if isinstance(refs_value, list) else []
        ordered_refs = sorted(
            enumerate(refs),
            key=lambda pair: (_positive_sequence(
                pair[1].get("sequence") if isinstance(pair[1], Mapping) else None,
                pair[0] + 1,
            ), pair[0]),
        )
        for ref_index, source_ref in ordered_refs:
            label = {
                "segment_id": segment_id or f"第 {segment_index + 1} 段",
                "source_index": ref_index,
            }
            if not isinstance(source_ref, Mapping):
                invalid.append({**label, "reason": "來源區域格式無效"})
                continue
            if source_ref.get("review_status", "confirmed") != "confirmed":
                skipped_unconfirmed.append(
                    {**label, "region_id": source_ref.get("region_id")}
                )
                continue
            region_id_value = source_ref.get("region_id")
            asset_id_value = source_ref.get("asset_id")
            region_id = (
                region_id_value.strip()
                if isinstance(region_id_value, str) and region_id_value.strip()
                else ""
            )
            asset_id = (
                asset_id_value.strip()
                if isinstance(asset_id_value, str) and asset_id_value.strip()
                else ""
            )
            if not segment_id:
                invalid.append({**label, "reason": "段落缺少 segment_id"})
                continue
            if not region_id:
                invalid.append({**label, "reason": "來源區域缺少 region_id"})
                continue
            if not asset_id or asset_id not in assets:
                invalid.append(
                    {**label, "region_id": region_id, "reason": "找不到來源照片"}
                )
                continue
            if not _valid_bbox(source_ref.get("bbox")):
                invalid.append(
                    {**label, "region_id": region_id, "reason": "來源框選座標無效"}
                )
                continue
            pair = (segment_id, region_id)
            if pair in seen_pairs:
                invalid.append(
                    {**label, "region_id": region_id, "reason": "同一段落的 region_id 重複"}
                )
                continue
            seen_pairs.add(pair)
            descriptor = {
                "segment_id": segment_id,
                "asset_id": asset_id,
                "region_id": region_id,
                "sequence": _positive_sequence(source_ref.get("sequence"), ref_index + 1),
                "source_ref": deepcopy(dict(source_ref)),
            }
            matching_runs = completed_runs.get(pair, [])
            current_completed = False
            for run in matching_runs:
                settings = run.get("recognition_settings")
                expected = run.get("target_fingerprint")
                if not isinstance(settings, Mapping) or not isinstance(expected, str):
                    continue
                if target_fingerprint(segment, source_ref, assets[asset_id], settings) == expected:
                    current_completed = True
                    break
            if current_completed:
                skipped_completed.append(descriptor)
                continue
            if matching_runs:
                # Old runs without a fingerprint, or results for an earlier
                # crop/bar structure, are evidence but are not safe proof that
                # the current target is complete.  They remain in project
                # history and the new request count is shown before any cost.
                stale_completed.append(descriptor)
            jobs.append(descriptor)

    return {
        "jobs": jobs,
        "segment_count": len({str(job["segment_id"]) for job in jobs}),
        "skipped_completed": skipped_completed,
        "stale_completed": stale_completed,
        "skipped_unconfirmed": skipped_unconfirmed,
        "invalid": invalid,
    }


def build_segment_context(
    segment: Mapping[str, object], source_ref: Mapping[str, object]
) -> dict[str, object]:
    """Build the provider context for one source region.

    New whole-score plans carry explicit Segment-local bar numbers.  The
    subtraction fallback exists only for older manually linked projects.
    """

    local_start = source_ref.get("segment_start_bar")
    local_end = source_ref.get("segment_end_bar")
    if local_start is None or local_end is None:
        try:
            source_start = int(segment.get("source_start_bar"))
            local_start = int(source_ref.get("source_start_bar")) - source_start + 1
            local_end = int(source_ref.get("source_end_bar")) - source_start + 1
        except (TypeError, ValueError):
            local_start = None
            local_end = None
    return {
        "segment_id": segment.get("segment_id"),
        "piece_name": segment.get("piece_name"),
        "section_name": segment.get("section_name"),
        "source_start_bar": segment.get("source_start_bar"),
        "source_end_bar": segment.get("source_end_bar"),
        "bars": segment.get("bars"),
        "time_signature": segment.get("time_signature"),
        "subdivision": segment.get("subdivision"),
        "bar_settings": deepcopy(segment.get("bar_settings", [])),
        "region_sequence": source_ref.get("sequence"),
        "region_source_start_bar": source_ref.get("source_start_bar"),
        "region_source_end_bar": source_ref.get("source_end_bar"),
        "region_segment_start_bar": local_start,
        "region_segment_end_bar": local_end,
        "event_tick_coordinate": "zero_based_within_bar",
    }


def target_fingerprint(
    segment: Mapping[str, object],
    source_ref: Mapping[str, object],
    asset: Mapping[str, object],
    settings: Mapping[str, object],
) -> str:
    """Fingerprint fields whose change would make a response unsafe to merge."""

    payload = {
        "segment": {
            "segment_id": segment.get("segment_id"),
            "source_start_bar": _canonical_intish(segment.get("source_start_bar")),
            "source_end_bar": _canonical_intish(segment.get("source_end_bar")),
            "bars": _canonical_intish(segment.get("bars")),
            "time_signature": segment.get("time_signature"),
            "subdivision": segment.get("subdivision"),
            "bar_settings": segment.get("bar_settings", []),
        },
        "source_ref": {
            key: (
                _canonical_intish(source_ref.get(key))
                if key
                in {
                    "sequence",
                    "source_start_bar",
                    "source_end_bar",
                    "segment_start_bar",
                    "segment_end_bar",
                }
                else source_ref.get(key)
            )
            for key in (
                "region_id",
                "asset_id",
                "bbox",
                "context_bbox",
                "sequence",
                "source_start_bar",
                "source_end_bar",
                "segment_start_bar",
                "segment_end_bar",
                "system_id",
            )
        },
        "asset": {
            "asset_id": asset.get("asset_id"),
            "sha256": asset.get("sha256"),
            "mime_type": asset.get("mime_type"),
            "width": asset.get("display_width", asset.get("width")),
            "height": asset.get("display_height", asset.get("height")),
        },
        "settings": {
            "model": settings.get("model"),
            "image_detail": settings.get("image_detail"),
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "COMPLETED_REGION_STATUSES",
    "RETRYABLE_REGION_STATUSES",
    "build_segment_context",
    "plan_recognition_batch",
    "summarize_batch_results",
    "target_fingerprint",
]
