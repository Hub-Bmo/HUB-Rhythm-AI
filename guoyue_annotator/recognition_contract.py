"""線上 AI 樂譜辨識的 provider-neutral JSON 契約。

這個模組只建立 request 並驗證 response，不含 HTTP、API key 或任何
供應商 SDK。未來的 provider adapter 可以把 request 送到任意服務，但
只有通過本模組驗證的 candidate 才會進入專案複核流程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from numbers import Real

from .instrument_library import UNKNOWN_ID, validate_library
from .photo_assets import SUPPORTED_IMAGE_MIME_TYPES
from .recognition import CANDIDATE_KINDS, new_recognition_candidate


CONTRACT_VERSION = "1.0"
NOTATION_PROFILES = frozenset({"auto", "staff", "numbered", "gongjing"})
COORDINATE_SPACE = "normalized_image"


class RecognitionContractError(ValueError):
    """Request 或 provider response 不符合辨識契約。"""


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RecognitionContractError(f"{field} 必須是正整數")
    return value


def _required_text(container: Mapping[str, object], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RecognitionContractError(f"{context} 缺少非空文字 {key}")
    return value.strip()


def _json_value(value: object, field: str = "value") -> object:
    """複製並正規化成可安全放進 JSON 的純資料。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecognitionContractError(f"{field} 不可含 NaN 或 Infinity")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise RecognitionContractError(f"{field} 的 JSON object key 必須是文字")
            result[key] = _json_value(child, f"{field}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(child, f"{field}[]") for child in value]
    raise RecognitionContractError(f"{field} 含有無法 JSON 序列化的型別")


def _bbox(value: object, context: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise RecognitionContractError(f"{context} 必須是 bbox object")
    required = {"x", "y", "width", "height"}
    if set(value) != required:
        raise RecognitionContractError(
            f"{context} 必須且只能包含 x、y、width、height"
        )
    if any(not _is_number(value[key]) for key in required):
        raise RecognitionContractError(f"{context} 的座標與寬高必須是有限數值")
    result = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    tolerance = 1e-9
    if result["x"] < 0 or result["y"] < 0:
        raise RecognitionContractError(f"{context} 的 x/y 不可為負")
    if result["width"] <= 0 or result["height"] <= 0:
        raise RecognitionContractError(f"{context} 的 width/height 必須大於 0")
    if result["x"] + result["width"] > 1 + tolerance:
        raise RecognitionContractError(f"{context} 超出照片右邊界")
    if result["y"] + result["height"] > 1 + tolerance:
        raise RecognitionContractError(f"{context} 超出照片下邊界")
    return result


def _contains(outer: Mapping[str, float], inner: Mapping[str, float]) -> bool:
    tolerance = 1e-9
    return (
        inner["x"] + tolerance >= outer["x"]
        and inner["y"] + tolerance >= outer["y"]
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tolerance
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tolerance
    )


def _request_instruments(snapshot: Mapping[str, object]) -> dict:
    # validate_library 同時檢查 ID 唯一性、別名碰撞與 stroke 隸屬。
    try:
        validate_library(snapshot, snapshot=True)
    except ValueError as error:
        raise RecognitionContractError(f"樂器快照無效：{error}") from error
    instruments: list[dict] = []
    for raw_instrument in snapshot.get("instruments", []):
        instrument = raw_instrument  # validated Mapping
        strokes: list[dict] = []
        for raw_stroke in instrument.get("strokes", []):  # type: ignore[union-attr]
            strokes.append(
                {
                    "stroke_id": raw_stroke["stroke_id"],
                    "code": raw_stroke["code"],
                    "name": raw_stroke["name"],
                    "aliases": list(raw_stroke.get("aliases", [])),
                }
            )
        instruments.append(
            {
                "instrument_id": instrument["instrument_id"],  # type: ignore[index]
                "name": instrument["name"],  # type: ignore[index]
                "aliases": list(instrument.get("aliases", [])),  # type: ignore[union-attr]
                "strokes": strokes,
            }
        )
    return {
        "schema_version": snapshot["schema_version"],
        "source_library_id": snapshot.get("source_library_id"),
        "source_revision": snapshot.get("source_revision"),
        "captured_at": snapshot.get("captured_at"),
        "instruments": instruments,
    }


def build_recognition_request(
    run: Mapping[str, object],
    asset: Mapping[str, object],
    instrument_snapshot: Mapping[str, object],
    *,
    region: Mapping[str, object] | None = None,
    notation_profile: str | None = None,
) -> dict:
    """建立不含本機路徑與憑證的嚴格 AI request。

    ``region`` 可以是 segment source_ref（含 ``region_id``/``bbox``），
    也可直接是 bbox。省略時會優先使用 run.source_region，否則視為整頁。
    """

    if not isinstance(run, Mapping) or not isinstance(asset, Mapping):
        raise RecognitionContractError("run 與 asset 必須是 JSON object")
    run_id = _required_text(run, "run_id", "run")
    run_asset_id = _required_text(run, "asset_id", "run")
    asset_id = _required_text(asset, "asset_id", "asset")
    if run_asset_id != asset_id:
        raise RecognitionContractError("run.asset_id 與 asset.asset_id 不一致")

    mime_type = _required_text(asset, "mime_type", "asset")
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise RecognitionContractError(f"不支援的照片 MIME：{mime_type}")
    sha256 = _required_text(asset, "sha256", "asset").lower()
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise RecognitionContractError("asset.sha256 必須是 64 位十六進位")
    # PhotoWorkspace uses the EXIF-corrected image for drawing normalized
    # regions.  Newer asset records therefore expose display dimensions; old
    # non-rotated projects continue to use their original dimensions.
    width = _positive_int(
        asset.get("display_width", asset.get("width")), "asset.display_width"
    )
    height = _positive_int(
        asset.get("display_height", asset.get("height")), "asset.display_height"
    )

    raw_region: object
    if region is not None:
        raw_region = region
    elif run.get("source_region") is not None:
        raw_region = run["source_region"]
    else:
        raw_region = {"x": 0, "y": 0, "width": 1, "height": 1}
    if isinstance(raw_region, Mapping) and "bbox" in raw_region:
        region_id_value = raw_region.get("region_id")
        region_bbox = _bbox(raw_region.get("bbox"), "region.bbox")
        context_bbox = (
            _bbox(raw_region.get("context_bbox"), "region.context_bbox")
            if raw_region.get("context_bbox") is not None
            else None
        )
    else:
        region_id_value = None
        region_bbox = _bbox(raw_region, "region")
        context_bbox = None
    region_id = (
        region_id_value.strip()
        if isinstance(region_id_value, str) and region_id_value.strip()
        else f"{run_id}-source"
    )

    profile = notation_profile
    if profile is None:
        profile = str(run.get("notation_profile") or asset.get("notation_profile") or "auto")
    if profile not in NOTATION_PROFILES:
        raise RecognitionContractError(f"無效的 notation_profile：{profile!r}")

    snapshot_payload = _request_instruments(instrument_snapshot)
    request = {
        "contract_version": CONTRACT_VERSION,
        "request_id": run_id,
        "task": "score_region_recognition",
        "source": {
            "asset": {
                "asset_id": asset_id,
                "sha256": sha256,
                "mime_type": mime_type,
                "width": width,
                "height": height,
            },
            "region": {
                "region_id": region_id,
                "coordinate_space": COORDINATE_SPACE,
                "bbox": region_bbox,
                **({"context_bbox": context_bbox} if context_bbox is not None else {}),
            },
        },
        "notation_profile": profile,
        "instrument_snapshot": snapshot_payload,
        "output_constraints": {
            "candidate_kinds": sorted(CANDIDATE_KINDS),
            "unknown_id": UNKNOWN_ID,
            "bbox_coordinate_space": COORDINATE_SPACE,
            "require_bbox": True,
            "require_source_evidence": True,
        },
    }
    # 在契約邊界確保資料是完全獨立的 JSON 值。
    return _json_value(request, "request")  # type: ignore[return-value]


def _allowed_ids(request: Mapping[str, object]) -> dict[str, set[str]]:
    snapshot = request.get("instrument_snapshot")
    if not isinstance(snapshot, Mapping):
        raise RecognitionContractError("request 缺少 instrument_snapshot")
    result: dict[str, set[str]] = {}
    instruments = snapshot.get("instruments")
    if not isinstance(instruments, list):
        raise RecognitionContractError("request.instrument_snapshot 格式無效")
    for instrument in instruments:
        if not isinstance(instrument, Mapping):
            raise RecognitionContractError("request 含有無效樂器")
        instrument_id = _required_text(instrument, "instrument_id", "request instrument")
        strokes = instrument.get("strokes")
        if not isinstance(strokes, list):
            raise RecognitionContractError(f"{instrument_id} 缺少 strokes")
        result[instrument_id] = {
            _required_text(stroke, "stroke_id", f"{instrument_id} stroke")
            for stroke in strokes
            if isinstance(stroke, Mapping)
        }
        if len(result[instrument_id]) != len(strokes):
            raise RecognitionContractError(f"{instrument_id} 含有無效 stroke")
    return result


def _validate_event_ids(data: Mapping[str, object], allowed: Mapping[str, set[str]]) -> None:
    instrument_id = _required_text(data, "instrument_id", "event data")
    stroke_id = _required_text(data, "stroke_id", "event data")
    if instrument_id == UNKNOWN_ID:
        if stroke_id != UNKNOWN_ID:
            raise RecognitionContractError("instrument_id=unknown 時 stroke_id 也必須是 unknown")
        return
    if instrument_id not in allowed:
        raise RecognitionContractError(f"event 使用快照外的 instrument_id：{instrument_id}")
    if stroke_id != UNKNOWN_ID and stroke_id not in allowed[instrument_id]:
        raise RecognitionContractError(
            f"event 的 stroke_id 不屬於 {instrument_id}：{stroke_id}"
        )


def validate_provider_response(
    request: Mapping[str, object], response: Mapping[str, object]
) -> list[dict]:
    """驗證 provider response 並轉成 :mod:`recognition` candidates。

    本函式不會自動將虛構 ID 改成 unknown；provider 必須明確回傳
    ``unknown``，否則整份 response 會被拒絕，以免情報無聲遺失。
    """

    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise RecognitionContractError("request/response 必須是 JSON object")
    if any(not isinstance(key, str) for key in response):
        raise RecognitionContractError("response 欄位名必須是文字")
    allowed_response_keys = {"contract_version", "request_id", "candidates", "provider_metadata"}
    unknown_response_keys = set(response) - allowed_response_keys
    if unknown_response_keys:
        raise RecognitionContractError(
            "response 含有契約外欄位：" + ", ".join(sorted(unknown_response_keys))
        )
    if response.get("contract_version") != CONTRACT_VERSION:
        raise RecognitionContractError("response contract_version 不相容")
    request_id = _required_text(request, "request_id", "request")
    if response.get("request_id") != request_id:
        raise RecognitionContractError("response.request_id 與 request 不一致")
    if "provider_metadata" in response:
        _json_value(response["provider_metadata"], "provider_metadata")
    raw_candidates = response.get("candidates")
    if not isinstance(raw_candidates, list):
        raise RecognitionContractError("response.candidates 必須是陣列")

    source = request.get("source")
    if not isinstance(source, Mapping):
        raise RecognitionContractError("request 缺少 source")
    asset = source.get("asset")
    region = source.get("region")
    if not isinstance(asset, Mapping) or not isinstance(region, Mapping):
        raise RecognitionContractError("request.source 格式無效")
    asset_id = _required_text(asset, "asset_id", "request source asset")
    region_id = _required_text(region, "region_id", "request source region")
    request_bbox = _bbox(region.get("bbox"), "request source region bbox")
    allowed_ids = _allowed_ids(request)

    result: list[dict] = []
    candidate_ids: set[str] = set()
    allowed_candidate_keys = {
        "candidate_id", "kind", "confidence", "bbox", "source_evidence", "data"
    }
    for index, raw_candidate in enumerate(raw_candidates):
        context = f"candidate[{index}]"
        if not isinstance(raw_candidate, Mapping):
            raise RecognitionContractError(f"{context} 必須是 JSON object")
        if any(not isinstance(key, str) for key in raw_candidate):
            raise RecognitionContractError(f"{context} 欄位名必須是文字")
        extra_keys = set(raw_candidate) - allowed_candidate_keys
        if extra_keys:
            raise RecognitionContractError(
                f"{context} 含有契約外欄位：" + ", ".join(sorted(extra_keys))
            )
        kind = _required_text(raw_candidate, "kind", context)
        if kind not in CANDIDATE_KINDS:
            raise RecognitionContractError(f"{context} 含有未允許的 kind：{kind}")
        confidence_value = raw_candidate.get("confidence")
        if not _is_number(confidence_value) or not 0 <= float(confidence_value) <= 1:
            raise RecognitionContractError(f"{context}.confidence 必須在 0 到 1 之間")
        candidate_bbox = _bbox(raw_candidate.get("bbox"), f"{context}.bbox")
        if not _contains(request_bbox, candidate_bbox):
            raise RecognitionContractError(f"{context}.bbox 不在 request region 內")

        evidence = raw_candidate.get("source_evidence")
        if not isinstance(evidence, Mapping):
            raise RecognitionContractError(f"{context} 缺少 source_evidence")
        evidence_copy = _json_value(evidence, f"{context}.source_evidence")
        assert isinstance(evidence_copy, dict)
        if evidence_copy.get("asset_id") != asset_id:
            raise RecognitionContractError(f"{context}.source_evidence.asset_id 不一致")
        if evidence_copy.get("region_id") != region_id:
            raise RecognitionContractError(f"{context}.source_evidence.region_id 不一致")

        data = raw_candidate.get("data")
        if not isinstance(data, Mapping):
            raise RecognitionContractError(f"{context}.data 必須是 JSON object")
        data_copy = _json_value(data, f"{context}.data")
        assert isinstance(data_copy, dict)
        if kind == "event":
            _validate_event_ids(data_copy, allowed_ids)

        raw_candidate_id = raw_candidate.get("candidate_id")
        if raw_candidate_id is not None and (
            not isinstance(raw_candidate_id, str) or not raw_candidate_id.strip()
        ):
            raise RecognitionContractError(f"{context}.candidate_id 必須是非空文字")
        candidate = new_recognition_candidate(
            request_id,
            kind,
            data_copy,
            candidate_id=raw_candidate_id.strip() if isinstance(raw_candidate_id, str) else None,
            confidence=float(confidence_value),
            bbox=candidate_bbox,
            source_asset_id=asset_id,
            source_evidence=evidence_copy,
        )
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id in candidate_ids:
            raise RecognitionContractError(
                f"response 含有重複的 candidate_id：{candidate_id}"
            )
        candidate_ids.add(candidate_id)
        result.append(candidate)
    return result


# 這個名稱在 provider adapter 讀起來比「validate」更直觀。
provider_response_to_candidates = validate_provider_response


__all__ = [
    "CONTRACT_VERSION",
    "COORDINATE_SPACE",
    "NOTATION_PROFILES",
    "RecognitionContractError",
    "build_recognition_request",
    "provider_response_to_candidates",
    "validate_provider_response",
]
