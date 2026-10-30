"""OpenAI Responses API adapter for photographed score recognition.

The rest of the application speaks the provider-neutral contract in
``recognition_contract``.  This module is deliberately the only place that
knows the OpenAI SDK request shape.  The SDK is imported lazily so opening a
project, reviewing previous results, and running the test suite do not require
an API key or even the optional ``openai`` package.

No API key is ever added to a prompt, provider payload, return value, or log.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from copy import deepcopy
from io import BytesIO
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .instrument_library import UNKNOWN_ID
from .recognition import CANDIDATE_KINDS
from .recognition_contract import (
    CONTRACT_VERSION,
    NOTATION_PROFILES,
    RecognitionContractError,
    validate_provider_response,
)


DEFAULT_MODEL = "gpt-6-astra"
# A photographed score can take noticeably longer than an ordinary text
# response.  Give the single request enough time to finish, while disabling
# the SDK's automatic retries below so one slow region cannot silently turn
# into several consecutive timeout waits.
DEFAULT_TIMEOUT_SECONDS = 300.0
SUPPORTED_IMAGE_DETAILS = frozenset({"low", "high", "original", "auto"})
OPENAI_SCHEMA_NAME = "guoyue_score_region_recognition"


class OpenAIProviderError(RuntimeError):
    """OpenAI adapter could not prepare, submit, or decode a request."""


class OpenAISDKUnavailableError(OpenAIProviderError):
    """The optional OpenAI Python SDK is not installed."""


def _json_copy(value: object, *, context: str) -> object:
    """Return a detached JSON value and reject NaN/custom objects early."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise OpenAIProviderError(f"{context} 必須是可序列化的 JSON 資料") from error


def _required_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OpenAIProviderError(f"{context} 必須是 JSON object")
    return value


def _required_text(container: Mapping[str, object], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OpenAIProviderError(f"{context} 缺少 {key}")
    return value.strip()


def _normalized_bbox(value: object, context: str) -> dict[str, float]:
    bbox = _required_mapping(value, context)
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        raw = bbox.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise OpenAIProviderError(f"{context}.{key} 必須是有限數值")
        number = float(raw)
        if not math.isfinite(number):
            raise OpenAIProviderError(f"{context}.{key} 必須是有限數值")
        result[key] = number
    if result["x"] < 0 or result["y"] < 0:
        raise OpenAIProviderError(f"{context} 的 x/y 不可為負")
    if result["width"] <= 0 or result["height"] <= 0:
        raise OpenAIProviderError(f"{context} 的寬高必須大於 0")
    if result["x"] + result["width"] > 1 + 1e-9:
        raise OpenAIProviderError(f"{context} 超出右邊界")
    if result["y"] + result["height"] > 1 + 1e-9:
        raise OpenAIProviderError(f"{context} 超出下邊界")
    return result


def _source_parts(
    request: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], dict[str, float]]:
    if request.get("contract_version") != CONTRACT_VERSION:
        raise OpenAIProviderError("辨識 request 的 contract_version 不相容")
    _required_text(request, "request_id", "request")
    source = _required_mapping(request.get("source"), "request.source")
    asset = _required_mapping(source.get("asset"), "request.source.asset")
    region = _required_mapping(source.get("region"), "request.source.region")
    _required_text(asset, "asset_id", "request.source.asset")
    _required_text(region, "region_id", "request.source.region")
    return asset, region, _normalized_bbox(
        region.get("bbox"), "request.source.region.bbox"
    )


def _flatten_transparency(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        white.alpha_composite(rgba)
        return white.convert("RGB")
    return image.convert("RGB")


def encode_score_region(
    request: Mapping[str, object], image_path: str | Path
) -> tuple[str, dict[str, object]]:
    """Crop the requested normalized region and return a PNG data URL.

    Pillow performs the crop even for a full-page request.  The model therefore
    always receives the exact same coordinate space: its candidate boxes are
    normalized to the submitted crop and are converted back to full-image
    coordinates before contract validation.
    """

    asset, _region, bbox = _source_parts(request)
    source = Path(image_path)
    if not source.is_file():
        raise FileNotFoundError(source)

    try:
        with Image.open(source) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            actual_width, actual_height = oriented.size
            expected_width = asset.get("width")
            expected_height = asset.get("height")
            if (
                isinstance(expected_width, int)
                and isinstance(expected_height, int)
                and (actual_width, actual_height)
                != (expected_width, expected_height)
            ):
                raise OpenAIProviderError(
                    "照片像素尺寸與專案資產紀錄不一致，請重新匯入照片"
                )

            left = max(0, min(actual_width - 1, math.floor(bbox["x"] * actual_width)))
            top = max(0, min(actual_height - 1, math.floor(bbox["y"] * actual_height)))
            right = max(
                left + 1,
                min(actual_width, math.ceil((bbox["x"] + bbox["width"]) * actual_width)),
            )
            bottom = max(
                top + 1,
                min(actual_height, math.ceil((bbox["y"] + bbox["height"]) * actual_height)),
            )
            cropped = _flatten_transparency(oriented.crop((left, top, right, bottom)))
            output = BytesIO()
            cropped.save(output, format="PNG")
    except OpenAIProviderError:
        raise
    except (OSError, ValueError) as error:
        raise OpenAIProviderError(f"無法讀取樂譜照片：{source.name}") from error

    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return (
        f"data:image/png;base64,{encoded}",
        {
            "pixel_box": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            },
            "pixel_width": right - left,
            "pixel_height": bottom - top,
            "source_bbox": bbox,
        },
    )


def _nullable(schema: Mapping[str, object]) -> dict[str, object]:
    return {"anyOf": [deepcopy(dict(schema)), {"type": "null"}]}


def _allowed_catalog_ids(request: Mapping[str, object]) -> tuple[list[str], list[str]]:
    snapshot = _required_mapping(
        request.get("instrument_snapshot"), "request.instrument_snapshot"
    )
    instruments = snapshot.get("instruments")
    if not isinstance(instruments, list):
        raise OpenAIProviderError("request.instrument_snapshot 缺少 instruments")
    instrument_ids = [UNKNOWN_ID]
    stroke_ids = [UNKNOWN_ID]
    for instrument in instruments:
        item = _required_mapping(instrument, "instrument_snapshot instrument")
        instrument_id = _required_text(item, "instrument_id", "instrument")
        if instrument_id not in instrument_ids:
            instrument_ids.append(instrument_id)
        strokes = item.get("strokes")
        if not isinstance(strokes, list):
            raise OpenAIProviderError(f"{instrument_id} 缺少 strokes")
        for stroke in strokes:
            stroke_item = _required_mapping(stroke, f"{instrument_id} stroke")
            stroke_id = _required_text(stroke_item, "stroke_id", "stroke")
            if stroke_id not in stroke_ids:
                stroke_ids.append(stroke_id)
    return instrument_ids, stroke_ids


def build_openai_response_schema(request: Mapping[str, object]) -> dict[str, object]:
    """Build the strict, request-specific schema sent to Structured Outputs."""

    instrument_ids, stroke_ids = _allowed_catalog_ids(request)
    nullable_nonnegative_integer = _nullable({"type": "integer", "minimum": 0})
    nullable_positive_integer = _nullable({"type": "integer", "minimum": 1})
    nullable_string = _nullable({"type": "string"})
    nullable_boolean = _nullable({"type": "boolean"})
    nullable_passes = _nullable(
        {"type": "array", "items": {"type": "integer", "minimum": 1}}
    )
    data_properties: dict[str, object] = {
        "instrument_id": {"type": "string", "enum": instrument_ids},
        "stroke_id": {"type": "string", "enum": stroke_ids},
        "bar": nullable_positive_integer,
        "tick": nullable_nonnegative_integer,
        "duration": nullable_positive_integer,
        "is_accent": nullable_boolean,
        "is_ornament": nullable_boolean,
        "is_roll": nullable_boolean,
        "type": _nullable(
            {
                "type": "string",
                "enum": ["repeat_start", "repeat_end", "volta", "other"],
            }
        ),
        "start_bar": nullable_positive_integer,
        "end_bar": nullable_positive_integer,
        "passes": nullable_passes,
        "times": nullable_positive_integer,
        "text": nullable_string,
        "raw_symbol": nullable_string,
        "note": nullable_string,
    }
    bbox_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1},
            "y": {"type": "number", "minimum": 0, "maximum": 1},
            "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        },
        "required": ["x", "y", "width", "height"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "detected_profile": {
                "type": "string",
                "enum": sorted(NOTATION_PROFILES),
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "candidate_id": {"type": "string", "minLength": 1},
                        "kind": {
                            "type": "string",
                            "enum": sorted(CANDIDATE_KINDS),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        # Coordinates in the exact crop submitted to OpenAI.
                        "crop_bbox": bbox_schema,
                        "observed_text": nullable_string,
                        "data": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": data_properties,
                            "required": list(data_properties),
                        },
                    },
                    "required": [
                        "candidate_id",
                        "kind",
                        "confidence",
                        "crop_bbox",
                        "observed_text",
                        "data",
                    ],
                },
            },
        },
        "required": ["detected_profile", "warnings", "candidates"],
    }


_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def _safe_segment_context(value: Mapping[str, object] | None) -> dict[str, object]:
    """Copy context while dropping fields that could accidentally contain secrets."""

    if value is None:
        return {}

    def scrub(item: object) -> object:
        if isinstance(item, Mapping):
            cleaned: dict[str, object] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                folded = key.casefold().replace("-", "_")
                if any(fragment in folded for fragment in _SENSITIVE_KEY_FRAGMENTS):
                    continue
                cleaned[key] = scrub(child)
            return cleaned
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [scrub(child) for child in item]
        return item

    safe = scrub(value)
    copied = _json_copy(safe, context="segment_context")
    assert isinstance(copied, dict)
    return copied


def build_recognition_prompt(
    request: Mapping[str, object],
    *,
    segment_context: Mapping[str, object] | None = None,
) -> str:
    """Create the score-specific prompt without local paths or credentials."""

    _asset, region, _bbox = _source_parts(request)
    profile = str(request.get("notation_profile") or "auto")
    snapshot = _required_mapping(
        request.get("instrument_snapshot"), "request.instrument_snapshot"
    )
    instrument_json = json.dumps(
        _json_copy(snapshot, context="instrument_snapshot"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    context_json = json.dumps(
        _safe_segment_context(segment_context),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    region_id = _required_text(region, "region_id", "request.source.region")
    context_note = (
        "第二張圖片是同一譜表系統的列名參考，包含左側樂器名稱；只可用來對應樂器列。"
        "所有節奏、bar 與 crop_bbox 一律以第一張主要裁切圖片為準，不得把第二張圖片的其他小節抄入結果。"
        if region.get("context_bbox") is not None
        else "本次只有一張主要裁切圖片。"
    )
    return f"""你是國樂打擊樂譜的辨識助理。請只報告照片中有直接視覺證據的候選，不要補寫或猜測看不清楚的音符。

此圖片是來源照片的框選區域（region_id={region_id}）。譜面可能是五線譜、數字譜／簡譜、鑼鼓經，指定 profile={profile}；若為 auto，請在 detected_profile 回報判斷結果。
圖片說明：{context_note}

工作原則：
1. 按樂器列、紙面小節與由左到右的節奏位置辨識。bar 必須是「目前 Segment 內」從 1 開始的小節號，不是印在紙上的絕對小節號；若段落脈絡含 region_segment_start_bar，裁切區第一個可見小節就從該值開始。tick 是該小節內從 0 開始的位置，每四分音符 960 tick，不得填整段的絕對 tick。無法可靠決定時使用 null 或不要建立候選。
2. event.data 的 instrument_id 與 stroke_id 只能逐字使用下方樂器快照中的穩定 ID；無法對應時兩者使用 unknown。不得自行創造 ID。
3. 辨識重音、裝飾音與滾奏／輪奏，分別填 is_accent、is_ornament、is_roll。滾奏與輪奏視為同一類。
4. 反覆記號建立 repeat 候選：data.type 使用 repeat_start、repeat_end 或 volta，並填相關 bar/start_bar/end_bar/passes/times。不要展開或複製事件。
5. 每個 crop_bbox 都以「本次送出的裁切圖片」為座標空間，x/y/width/height 正規化到 0..1，且不得超出圖片。
6. observed_text 放實際看見的字、譜號或符號；不確定處寫入 warnings。confidence 是視覺辨識信心，不是整體美觀評分。
7. 在數字譜／簡譜中，0 表示休止：不建立 event 候選，但後續事件的 tick 仍須保留這段時間。其他數字 N 只有在所屬樂器快照中有明確對應擊法時才使用該 stroke_id；例如定音鼓的 N 應對應快照中的 pitch_N／音 N，不可自行創造 ID。

樂器與擊法快照：{instrument_json}
目前段落脈絡：{context_json}
"""


def build_openai_request(
    request: Mapping[str, object],
    image_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    detail: str = "high",
    segment_context: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return ``responses.create`` kwargs plus local crop metadata."""

    if not isinstance(model, str) or not model.strip():
        raise OpenAIProviderError("OpenAI model 不可為空")
    if detail not in SUPPORTED_IMAGE_DETAILS:
        raise OpenAIProviderError(
            f"detail 必須是 {', '.join(sorted(SUPPORTED_IMAGE_DETAILS))}"
        )
    data_url, crop_metadata = encode_score_region(request, image_path)
    schema = build_openai_response_schema(request)
    image_content: list[dict[str, object]] = [
        {
            "type": "input_image",
            "image_url": data_url,
            "detail": detail,
        }
    ]
    _asset, region, _bbox = _source_parts(request)
    context_bbox = region.get("context_bbox")
    if context_bbox is not None:
        normalized_context = _normalized_bbox(
            context_bbox, "request.source.region.context_bbox"
        )
        context_request = deepcopy(dict(request))
        context_request["source"]["region"]["bbox"] = normalized_context
        context_url, _context_metadata = encode_score_region(
            context_request, image_path
        )
        image_content.append(
            {
                "type": "input_image",
                "image_url": context_url,
                "detail": detail,
            }
        )
    payload: dict[str, object] = {
        "model": model.strip(),
        "store": False,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": build_recognition_prompt(
                            request, segment_context=segment_context
                        ),
                    },
                    *image_content,
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": OPENAI_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            }
        },
    }
    return payload, crop_metadata


def _new_client(api_key: str | None, timeout: float) -> object:
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as error:
        raise OpenAISDKUnavailableError(
            "尚未安裝 OpenAI Python SDK，無法執行線上辨識"
        ) from error

    kwargs: dict[str, object] = {
        "timeout": timeout,
        # The OpenAI Python SDK retries timeout and selected transient errors
        # twice by default.  Batch recognition already records each region as
        # failed and lets the user retry only unfinished regions, so retries
        # belong at that visible job level instead of being hidden here.
        "max_retries": 0,
    }
    if isinstance(api_key, str) and api_key.strip():
        kwargs["api_key"] = api_key.strip()
    try:
        return OpenAI(**kwargs)
    except Exception as error:  # SDK configuration errors have version-specific types.
        # Do not echo SDK exception text: configuration errors can contain a
        # partially displayed credential in some SDK versions.
        raise OpenAIProviderError("無法建立 OpenAI client，請檢查 API key 與設定") from error


def _value(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _extract_output_text(response: object) -> str:
    direct = _value(response, "output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    # Compatibility with simple fakes and SDK response objects that expose the
    # typed output tree but not the convenience property.
    parts: list[str] = []
    output = _value(response, "output", [])
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes, bytearray)):
        for item in output:
            content = _value(item, "content", [])
            if not isinstance(content, Sequence) or isinstance(
                content, (str, bytes, bytearray)
            ):
                continue
            for block in content:
                block_type = _value(block, "type")
                text = _value(block, "text")
                if block_type in {"output_text", "text"} and isinstance(text, str):
                    parts.append(text)
    combined = "".join(parts).strip()
    if not combined:
        raise OpenAIProviderError("OpenAI 回應沒有可解析的 output_text")
    return combined


def _to_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        try:
            return _json_copy(value, context="OpenAI response metadata")
        except OpenAIProviderError:
            return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_copy(model_dump(), context="OpenAI response metadata")
        except (OpenAIProviderError, TypeError, ValueError):
            return None
    return None


def _prune_nulls(data: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "type" and value == "other":
            continue
        result[str(key)] = deepcopy(value)
    return result


def _crop_bbox_to_source(
    crop_bbox: Mapping[str, object], source_bbox: Mapping[str, float]
) -> dict[str, float]:
    local = _normalized_bbox(crop_bbox, "candidate.crop_bbox")
    return {
        "x": source_bbox["x"] + local["x"] * source_bbox["width"],
        "y": source_bbox["y"] + local["y"] * source_bbox["height"],
        "width": local["width"] * source_bbox["width"],
        "height": local["height"] * source_bbox["height"],
    }


def _provider_payload_from_model(
    request: Mapping[str, object],
    model_output: Mapping[str, object],
    *,
    model: str,
    response_id: str | None,
) -> tuple[dict[str, object], str, list[str]]:
    _asset, region, source_bbox = _source_parts(request)
    source = _required_mapping(request.get("source"), "request.source")
    asset = _required_mapping(source.get("asset"), "request.source.asset")
    asset_id = _required_text(asset, "asset_id", "request.source.asset")
    region_id = _required_text(region, "region_id", "request.source.region")

    detected_profile = model_output.get("detected_profile")
    if detected_profile not in NOTATION_PROFILES:
        raise OpenAIProviderError("OpenAI 回傳無效的 detected_profile")
    raw_warnings = model_output.get("warnings")
    if not isinstance(raw_warnings, list) or not all(
        isinstance(item, str) for item in raw_warnings
    ):
        raise OpenAIProviderError("OpenAI 回傳無效的 warnings")
    raw_candidates = model_output.get("candidates")
    if not isinstance(raw_candidates, list):
        raise OpenAIProviderError("OpenAI 回傳無效的 candidates")

    converted: list[dict[str, object]] = []
    for index, raw in enumerate(raw_candidates):
        item = _required_mapping(raw, f"OpenAI candidate[{index}]")
        data = _required_mapping(item.get("data"), f"OpenAI candidate[{index}].data")
        crop_bbox = _normalized_bbox(
            item.get("crop_bbox"), f"OpenAI candidate[{index}].crop_bbox"
        )
        observed_text = item.get("observed_text")
        if observed_text is not None and not isinstance(observed_text, str):
            raise OpenAIProviderError(
                f"OpenAI candidate[{index}].observed_text 格式無效"
            )
        evidence: dict[str, object] = {
            "asset_id": asset_id,
            "region_id": region_id,
            "crop_bbox": crop_bbox,
        }
        if observed_text is not None:
            evidence["observed_text"] = observed_text
        converted.append(
            {
                "candidate_id": item.get("candidate_id"),
                "kind": item.get("kind"),
                "confidence": item.get("confidence"),
                "bbox": _crop_bbox_to_source(crop_bbox, source_bbox),
                "source_evidence": evidence,
                "data": _prune_nulls(data),
            }
        )

    metadata: dict[str, object] = {"provider": "openai", "model": model}
    if response_id:
        metadata["response_id"] = response_id
    provider_payload: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "request_id": _required_text(request, "request_id", "request"),
        "candidates": converted,
        "provider_metadata": metadata,
    }
    return provider_payload, str(detected_profile), list(raw_warnings)


def recognize_score_region(
    request: Mapping[str, object],
    image_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    detail: str = "high",
    segment_context: Mapping[str, object] | None = None,
    client: object | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Recognize one score region with OpenAI and validate every candidate.

    ``client`` is injectable for deterministic offline tests.  With no client,
    the optional SDK is imported and
    ``OpenAI(api_key=..., timeout=..., max_retries=0)`` is constructed only
    when this function is called.  Disabling SDK retries keeps one region to
    one network attempt; unfinished regions can be retried explicitly by the
    application's batch workflow.
    """

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise OpenAIProviderError("timeout 必須是正數秒數")
    if not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise OpenAIProviderError("timeout 必須是正數秒數")

    create_kwargs, _crop_metadata = build_openai_request(
        request,
        image_path,
        model=model,
        detail=detail,
        segment_context=segment_context,
    )
    active_client = client if client is not None else _new_client(api_key, float(timeout))
    responses = getattr(active_client, "responses", None)
    create = getattr(responses, "create", None)
    if not callable(create):
        raise OpenAIProviderError("OpenAI client 缺少 responses.create")

    try:
        response = create(**create_kwargs)
    except OpenAIProviderError:
        raise
    except Exception as error:
        # Likewise, keep the original exception only as the chained cause;
        # application-visible text must never persist a credential.
        raise OpenAIProviderError(
            f"OpenAI 辨識請求失敗（{type(error).__name__}）"
        ) from error

    output_text = _extract_output_text(response)
    try:
        decoded = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise OpenAIProviderError("OpenAI 回應不是有效的 JSON") from error
    if not isinstance(decoded, Mapping):
        raise OpenAIProviderError("OpenAI 回應最外層必須是 JSON object")

    raw_response_id = _value(response, "id")
    response_id = raw_response_id if isinstance(raw_response_id, str) else None
    provider_payload, detected_profile, warnings = _provider_payload_from_model(
        request,
        decoded,
        model=model.strip(),
        response_id=response_id,
    )
    try:
        candidates = validate_provider_response(request, provider_payload)
    except RecognitionContractError as error:
        raise OpenAIProviderError(f"OpenAI 辨識結果未通過資料契約：{error}") from error

    usage = _to_json_value(_value(response, "usage"))
    return {
        "candidates": candidates,
        "response_id": response_id,
        "usage": usage,
        "warnings": warnings,
        "detected_profile": detected_profile,
        "raw_provider_payload": provider_payload,
    }


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "OPENAI_SCHEMA_NAME",
    "OpenAIProviderError",
    "OpenAISDKUnavailableError",
    "SUPPORTED_IMAGE_DETAILS",
    "build_openai_request",
    "build_openai_response_schema",
    "build_recognition_prompt",
    "encode_score_region",
    "recognize_score_region",
]
