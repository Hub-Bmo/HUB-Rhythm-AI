"""AI 樂譜辨識草稿與人工複核狀態的純資料 helper。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from numbers import Real
from typing import Iterable, Mapping
from uuid import uuid4


REVIEW_DECISIONS = frozenset({"accepted", "corrected", "rejected"})
CANDIDATE_KINDS = frozenset(
    {"event", "instrument", "stroke", "repeat", "barline", "text", "other"}
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


def _normalize_bbox(bbox: Mapping[str, Real] | None) -> dict | None:
    if bbox is None:
        return None
    keys = ("x", "y", "width", "height")
    if any(key not in bbox or not isinstance(bbox[key], Real) for key in keys):
        raise ValueError("bbox 必須包含數值 x、y、width、height")
    result = {key: float(bbox[key]) for key in keys}
    if result["x"] < 0 or result["y"] < 0 or result["width"] <= 0 or result["height"] <= 0:
        raise ValueError("bbox 的位置不可為負，寬高必須大於 0")
    return result


def new_review_progress(candidates: Iterable[Mapping[str, object]] = ()) -> dict:
    """從 candidate 即時計算複核進度，不依賴可能過期的儲存計數。"""

    counts = {"accepted": 0, "corrected": 0, "rejected": 0, "pending": 0}
    candidate_list = list(candidates)
    for candidate in candidate_list:
        status = candidate.get("review_status", "pending")
        if not isinstance(status, str) or status not in counts:
            status = "pending"
        counts[status] += 1
    reviewed = counts["accepted"] + counts["corrected"] + counts["rejected"]
    total = len(candidate_list)
    return {
        "total": total,
        "reviewed": reviewed,
        "pending": counts["pending"],
        "accepted": counts["accepted"],
        "corrected": counts["corrected"],
        "rejected": counts["rejected"],
        "fraction": (reviewed / total) if total else 0.0,
        "is_complete": total > 0 and reviewed == total,
    }


def new_recognition_run(
    asset_id: str,
    *,
    run_id: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    source_region: Mapping[str, Real] | None = None,
    created_at: str | None = None,
) -> dict:
    """建立一次辨識的 JSON 可序列化資料。"""

    if not asset_id:
        raise ValueError("asset_id 不可為空")
    timestamp = created_at or _now_iso()
    return {
        "run_id": run_id or _identifier("RUN"),
        "asset_id": asset_id,
        "engine": engine,
        "model": model,
        "source_region": _normalize_bbox(source_region),
        "status": "pending",
        "error": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "candidates": [],
        "review_progress": new_review_progress(),
    }


def new_recognition_candidate(
    run_id: str,
    kind: str,
    data: Mapping[str, object],
    *,
    candidate_id: str | None = None,
    confidence: float | None = None,
    bbox: Mapping[str, Real] | None = None,
    source_asset_id: str | None = None,
    source_evidence: Mapping[str, object] | None = None,
    created_at: str | None = None,
) -> dict:
    """建立一個尚未複核的 AI 候選項目。"""

    if not run_id:
        raise ValueError("run_id 不可為空")
    # 常用類型列在 CANDIDATE_KINDS 供 UI 建議，但線上辨識器未來可能
    # 增加新符號；資料層只要求非空文字，不讓新類型無法保存。
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("candidate kind 不可為空")
    if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence 必須在 0 到 1 之間")
    return {
        "candidate_id": candidate_id or _identifier("CAND"),
        "run_id": run_id,
        "source_asset_id": source_asset_id,
        "kind": kind.strip(),
        "confidence": None if confidence is None else float(confidence),
        "bbox": _normalize_bbox(bbox),
        "source_evidence": (
            None if source_evidence is None else deepcopy(dict(source_evidence))
        ),
        "data": deepcopy(dict(data)),
        "review_status": "pending",
        "review": None,
        "created_at": created_at or _now_iso(),
    }


def review_candidate(
    candidate: Mapping[str, object],
    decision: str,
    *,
    corrected_data: Mapping[str, object] | None = None,
    note: str = "",
    reviewed_at: str | None = None,
) -> dict:
    """回傳已複核的新 candidate，不修改傳入的原始 dict。"""

    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"無效的複核決定: {decision}")
    if decision == "corrected" and corrected_data is None:
        raise ValueError("corrected 必須附 corrected_data")
    if decision != "corrected" and corrected_data is not None:
        raise ValueError("只有 corrected 可以附 corrected_data")

    result = deepcopy(dict(candidate))
    result["review_status"] = decision
    result["review"] = {
        "decision": decision,
        "corrected_data": None if corrected_data is None else deepcopy(dict(corrected_data)),
        "note": str(note),
        "reviewed_at": reviewed_at or _now_iso(),
    }
    return result


def update_recognition_run(
    run: Mapping[str, object],
    candidates: Iterable[Mapping[str, object]],
    *,
    updated_at: str | None = None,
) -> dict:
    """回傳包含最新 candidates、狀態與複核進度的新 run。"""

    result = deepcopy(dict(run))
    result["candidates"] = [deepcopy(dict(candidate)) for candidate in candidates]
    progress = new_review_progress(result["candidates"])
    result["review_progress"] = progress
    if progress["is_complete"]:
        result["status"] = "reviewed"
    elif progress["total"]:
        result["status"] = "ready_for_review"
    else:
        result["status"] = "pending"
    result["updated_at"] = updated_at or _now_iso()
    return result


__all__ = [
    "CANDIDATE_KINDS",
    "REVIEW_DECISIONS",
    "new_recognition_candidate",
    "new_recognition_run",
    "new_review_progress",
    "review_candidate",
    "update_recognition_run",
]
