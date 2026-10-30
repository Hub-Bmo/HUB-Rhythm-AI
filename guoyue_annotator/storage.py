"""專案存檔、載入，以及訓練資料匯出。"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import tempfile

from .models import (
    SCHEMA_VERSION,
    ensure_project_event_annotations,
    new_project,
    now_iso,
    selected_take,
)
from .score_plan import migrate_page_metadata_in_place, migrate_source_refs_in_place


PROJECT_MANIFEST = "project.json"
PROJECT_BUNDLE_SUFFIX = ".guoyue-project"


def project_manifest_path(path: str | Path) -> Path:
    """Return the JSON manifest for either a legacy file or a project folder."""
    path = Path(path)
    return (
        path / PROJECT_MANIFEST
        if path.is_dir() or path.suffix.lower() == PROJECT_BUNDLE_SUFFIX
        else path
    )


def project_root(path: str | Path) -> Path:
    path = project_manifest_path(path)
    return path.parent


def migrate_project(project: dict) -> dict:
    """Upgrade legacy dict projects in place without discarding unknown fields."""
    if int(project.get("ppq", 0)) != 960:
        raise ValueError("此工具目前只接受 PPQ=960 的專案")
    if not isinstance(project.get("segments"), list):
        raise ValueError("專案檔缺少 segments 清單")

    defaults = new_project()
    for key in (
        "assets",
        "recognition_runs",
        "score_plan",
        "instrument_library_snapshot",
        "workspace_state",
    ):
        project.setdefault(key, defaults[key])
    score_plan = project.get("score_plan")
    if isinstance(score_plan, dict):
        score_plan.setdefault("excluded_asset_ids", [])
    project.setdefault("input_mode", "mixed")
    project.setdefault("next_event_number", 1)
    ensure_project_event_annotations(project)
    migrate_page_metadata_in_place(project["assets"])

    highest_event_number = 0
    for segment in project.get("segments", []):
        segment.setdefault("source_refs", [])
        migrate_source_refs_in_place(
            segment["source_refs"],
            legacy_start_bar=segment.get("source_start_bar", ""),
            legacy_end_bar=segment.get("source_end_bar", ""),
        )
        segment.setdefault("navigation", {
            "markers": [],
            "playback_order": [],
            "review_status": "not_started",
        })
        segment.setdefault("review", {
            "status": "not_started",
            "by_track": {},
            "updated_at": None,
        })
        for track in segment.get("tracks", []):
            for brush in track.get("brushes", []):
                if isinstance(brush, dict):
                    brush.setdefault("stroke_id", None)
            for take in track.get("takes", []):
                take.setdefault("review_status", "accepted")
                for event in take.get("events", []):
                    event.setdefault("stroke_id", None)
                    event.setdefault("duration", None)
                    event_id = str(event.get("event_id", ""))
                    if event_id.startswith("E") and event_id[1:].isdigit():
                        highest_event_number = max(highest_event_number, int(event_id[1:]))
    project["next_event_number"] = max(
        int(project.get("next_event_number", 1)), highest_event_number + 1
    )
    project["schema_version"] = SCHEMA_VERSION
    return project


def save_project(project: dict, path: str | Path) -> None:
    path = project_manifest_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrate_project(project)
    project["updated_at"] = now_iso()
    payload = json.dumps(project, ensure_ascii=False, indent=2)
    backup = path.with_suffix(path.suffix + ".bak")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        if path.exists():
            shutil.copy2(path, backup)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_project(path: str | Path) -> dict:
    path = project_manifest_path(path)
    project = json.loads(path.read_text(encoding="utf-8"))
    project = migrate_project(project)
    # A saved ``running`` state means the previous desktop process exited
    # before it could merge a provider result. Never pretend that request is
    # still alive after restart; keep it visible and safe to retry.
    for run in project.get("recognition_runs", []):
        if isinstance(run, dict) and run.get("status") == "running":
            run["status"] = "interrupted"
            run["error"] = {
                "type": "Interrupted",
                "message": "程式上次在辨識完成前關閉；請重新送出這個區域。",
            }
    return project


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_dataset(project: dict, directory: str | Path) -> list[Path]:
    """匯出正規化 CSV 與每段一列的 JSONL 訓練資料。"""
    ensure_project_event_annotations(project)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    segment_fields = [
        "segment_id", "piece_id", "piece_name", "pattern_name", "section_name",
        "selection_type", "selection_reason", "source", "source_page",
        "source_start_bar", "source_end_bar", "system", "time_signature",
        "tempo", "bars", "ppq", "subdivision", "bar_settings_json", "version", "notes",
        "created_at", "updated_at",
    ]
    track_fields = [
        "track_id", "segment_id", "instrument_id", "instrument", "sound_profile", "track_order",
        "is_complete", "selected_take_id", "notes",
    ]
    take_fields = [
        "take_id", "track_id", "segment_id", "created_at", "quantization",
        "count_in_bars", "output_latency_ms", "input_method", "event_count", "is_selected",
    ]
    event_fields = [
        "event_id", "segment_id", "track_id", "take_id", "instrument", "bar",
        "beat", "tick", "raw_tick", "onset_ms", "stroke_id", "stroke", "is_accent",
        "is_ornament", "is_roll", "duration",
        "velocity", "key_code", "input_group", "quantization", "grid_index",
        "input_method",
    ]

    segment_rows: list[dict] = []
    track_rows: list[dict] = []
    take_rows: list[dict] = []
    event_rows: list[dict] = []
    jsonl_rows: list[dict] = []

    for segment in project.get("segments", []):
        segment_row = dict(segment)
        segment_row["bar_settings_json"] = json.dumps(
            segment.get("bar_settings", []), ensure_ascii=False, separators=(",", ":")
        )
        segment_rows.append(segment_row)
        sample = {key: segment.get(key) for key in segment_fields}
        sample.pop("bar_settings_json", None)
        sample["bar_settings"] = segment.get("bar_settings", [])
        sample["tracks"] = []
        for track in segment.get("tracks", []):
            track_rows.append(track)
            chosen = selected_take(track)
            for take in track.get("takes", []):
                take_rows.append({
                    **take,
                    "track_id": track["track_id"],
                    "segment_id": segment["segment_id"],
                    "event_count": len(take.get("events", [])),
                    "is_selected": take.get("take_id") == track.get("selected_take_id"),
                })
            chosen_events = [] if chosen is None else chosen.get("events", [])
            for event in chosen_events:
                event_rows.append({**event, "instrument": track["instrument"]})
            sample["tracks"].append({
                "track_id": track["track_id"],
                "instrument_id": track.get("instrument_id"),
                "instrument": track["instrument"],
                "is_complete": bool(track.get("is_complete")),
                "selected_take_id": track.get("selected_take_id"),
                "events": chosen_events,
            })
        jsonl_rows.append(sample)

    paths = [
        directory / "segments.csv",
        directory / "tracks.csv",
        directory / "takes.csv",
        directory / "events.csv",
        directory / "dataset.jsonl",
    ]
    _write_csv(paths[0], segment_fields, segment_rows)
    _write_csv(paths[1], track_fields, track_rows)
    _write_csv(paths[2], take_fields, take_rows)
    _write_csv(paths[3], event_fields, event_rows)
    with paths[4].open("w", encoding="utf-8") as file:
        for row in jsonl_rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
    return paths
