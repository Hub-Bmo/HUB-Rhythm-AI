"""純本地的樂譜頁面基礎版面分析。

這個模組只依賴 Pillow，不呼叫網路或 AI API。它的輸出是保守的
「候選」：先找出可能的譜面內容區，再用水平／垂直投影估計譜面系統
與小節線。當圖片資訊不足時，回傳空候選與 warning，不會為了填滿
結果而虛構邊界。

所有 bbox 都使用 EXIF 方向校正後的圖片座標，並正規化到 0..1，
可直接 JSON 序列化。
"""

from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
import statistics

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError


LAYOUT_SCHEMA_VERSION = "1.0"
COORDINATE_SPACE = "normalized_image"
SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG"})


class ScoreLayoutError(ValueError):
    """圖片無法進入本地版面分析。"""


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _percentile_from_histogram(histogram: Sequence[int], fraction: float) -> int:
    total = sum(histogram)
    if total <= 0:
        return 0
    target = max(1, math.ceil(total * _clamp(fraction)))
    seen = 0
    for value, count in enumerate(histogram):
        seen += int(count)
        if seen >= target:
            return value
    return len(histogram) - 1


def _weighted_bounds(
    projection: Sequence[int],
    *,
    lower_fraction: float = 0.003,
    upper_fraction: float = 0.997,
) -> tuple[int, int] | None:
    """找出包含投影主要質量的 [start, end) 區間。

    少量單點塵污會被切掉，但不用「必須連續」的假設，因此適用於
    本來就含有大量空白的樂譜。
    """

    total = sum(projection)
    if total <= 0:
        return None
    lower_target = total * lower_fraction
    upper_target = total * upper_fraction
    seen = 0
    start = 0
    end = len(projection)
    for index, count in enumerate(projection):
        seen += count
        if seen >= lower_target:
            start = index
            break
    seen = 0
    for index, count in enumerate(projection):
        seen += count
        if seen >= upper_target:
            end = index + 1
            break
    return (start, max(start + 1, end))


def _runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    start: int | None = None
    for index, active in enumerate(flags):
        if active and start is None:
            start = index
        elif not active and start is not None:
            result.append((start, index))
            start = None
    if start is not None:
        result.append((start, len(flags)))
    return result


def _bridge_short_gaps(flags: Sequence[bool], maximum_gap: int) -> list[bool]:
    result = list(flags)
    active_runs = _runs(result)
    for left, right in zip(active_runs, active_runs[1:]):
        if right[0] - left[1] <= maximum_gap:
            result[left[1] : right[0]] = [True] * (right[0] - left[1])
    return result


def _normalized_bbox(
    left: int,
    top: int,
    right: int,
    bottom: int,
    image_width: int,
    image_height: int,
) -> dict[str, float]:
    left = max(0, min(image_width - 1, left))
    top = max(0, min(image_height - 1, top))
    right = max(left + 1, min(image_width, right))
    bottom = max(top + 1, min(image_height, bottom))
    return {
        "x": round(left / image_width, 6),
        "y": round(top / image_height, 6),
        "width": round((right - left) / image_width, 6),
        "height": round((bottom - top) / image_height, 6),
    }


def _warning(code: str, message: str, *, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _longest_true_run(values: Sequence[bool]) -> int:
    longest = 0
    current = 0
    for value in values:
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _mask_projections(mask: Image.Image) -> tuple[list[int], list[int], int]:
    width, height = mask.size
    rows = [0] * height
    columns = [0] * width
    total = 0
    data = mask.tobytes()
    for offset, value in enumerate(data):
        if value:
            y, x = divmod(offset, width)
            rows[y] += 1
            columns[x] += 1
            total += 1
    return rows, columns, total


def _region_projections(
    mask: Image.Image, left: int, top: int, right: int, bottom: int
) -> tuple[list[int], list[int]]:
    crop = mask.crop((left, top, right, bottom))
    rows, columns, _total = _mask_projections(crop)
    return rows, columns


def _candidate_system_ranges(
    mask: Image.Image, content_box: tuple[int, int, int, int]
) -> list[tuple[int, int, int, int]]:
    left, top, right, bottom = content_box
    content_width = right - left
    content_height = bottom - top
    row_counts, _columns = _region_projections(mask, left, top, right, bottom)
    minimum_row_ink = max(2, round(content_width * 0.0025))
    active = [count >= minimum_row_ink for count in row_counts]
    active = _bridge_short_gaps(active, max(2, round(content_height * 0.003)))
    bands = _runs(active)
    if not bands:
        return []

    # 譜面系統間的空白通常顯著大於五線、樂器列或文字行間距。
    # 使用頁面比例與中位間距兩個下限，避免把單一總譜的樂器列
    # 錯切成許多個 system。
    gaps = [right_band[0] - left_band[1] for left_band, right_band in zip(bands, bands[1:])]
    positive_gaps = [gap for gap in gaps if gap > 0]
    # 只有一、兩個空白時，它們往往就是真正的 system 分隔；
    # 樣本太少時不能反過來把該間距當成「一般行距」。
    median_gap = statistics.median(positive_gaps) if len(positive_gaps) >= 3 else 0.0
    split_gap = max(
        12,
        round(content_height * 0.055),
        round(median_gap * 3.2),
    )

    grouped: list[tuple[int, int]] = []
    group_start, group_end = bands[0]
    for band in bands[1:]:
        if band[0] - group_end >= split_gap:
            grouped.append((group_start, group_end))
            group_start = band[0]
        group_end = band[1]
    grouped.append((group_start, group_end))

    result: list[tuple[int, int, int, int]] = []
    vertical_padding = max(2, round(content_height * 0.008))
    for local_top, local_bottom in grouped:
        candidate_top = max(top, top + local_top - vertical_padding)
        candidate_bottom = min(bottom, top + local_bottom + vertical_padding)
        candidate_rows, candidate_columns = _region_projections(
            mask, left, candidate_top, right, candidate_bottom
        )
        horizontal_bounds = _weighted_bounds(
            candidate_columns, lower_fraction=0.006, upper_fraction=0.994
        )
        if horizontal_bounds is None:
            continue
        local_left, local_right = horizontal_bounds
        candidate_left = max(left, left + local_left - max(2, round(content_width * 0.008)))
        candidate_right = min(
            right, left + local_right + max(2, round(content_width * 0.008))
        )
        width_ratio = (candidate_right - candidate_left) / max(1, content_width)
        height_ratio = (candidate_bottom - candidate_top) / max(1, content_height)
        # 頁碼、曲名或單行註解不應被當成橫跨頁面的譜面系統。
        if width_ratio < 0.42 or height_ratio < 0.035:
            continue
        result.append((candidate_left, candidate_top, candidate_right, candidate_bottom))
    return result


def _barline_ranges(
    mask: Image.Image, system_box: tuple[int, int, int, int]
) -> list[tuple[tuple[int, int, int, int], float]]:
    left, top, right, bottom = system_box
    width = right - left
    height = bottom - top
    _row_counts, column_counts = _region_projections(mask, left, top, right, bottom)
    if not column_counts or height < 4:
        return []

    median_count = float(statistics.median(column_counts))
    median_coverage = median_count / height
    minimum_count = max(
        4,
        math.ceil(height * 0.075),
        math.ceil(median_count * 1.8),
    )
    pixels = mask.load()
    qualifying: list[bool] = []
    column_evidence: list[tuple[float, float]] = []
    for local_x, count in enumerate(column_counts):
        x = left + local_x
        vertical_flags = [bool(pixels[x, y]) for y in range(top, bottom)]
        coverage = count / height
        continuity = _longest_true_run(vertical_flags) / height
        enough_ink = count >= minimum_count
        clearly_vertical = continuity >= 0.06 or coverage >= 0.14
        qualifying.append(enough_ink and clearly_vertical)
        column_evidence.append((coverage, continuity))

    qualifying = _bridge_short_gaps(qualifying, 1)
    groups = _runs(qualifying)
    maximum_width = max(3, round(width * 0.012))
    result: list[tuple[tuple[int, int, int, int], float]] = []
    for group_left, group_right in groups:
        if group_right - group_left > maximum_width:
            # 寬文字、大括弧或照片折痕不是單一小節線。
            continue
        evidence = column_evidence[group_left:group_right]
        peak_coverage = max(item[0] for item in evidence)
        peak_continuity = max(item[1] for item in evidence)
        relative_prominence = peak_coverage / max(0.04, median_coverage + 0.025)
        vertical_strength = max(
            _clamp(peak_coverage / 0.24),
            _clamp(peak_continuity / 0.5),
        )
        confidence = _clamp(
            0.38
            + 0.30 * _clamp((relative_prominence - 1.0) / 3.0)
            + 0.30 * vertical_strength,
            0.0,
            0.96,
        )
        if confidence < 0.5:
            continue

        absolute_left = left + group_left
        absolute_right = left + group_right
        y_projection = [0] * height
        for y in range(top, bottom):
            y_projection[y - top] = sum(
                1 for x in range(absolute_left, absolute_right) if pixels[x, y]
            )
        y_bounds = _weighted_bounds(
            y_projection, lower_fraction=0.01, upper_fraction=0.99
        )
        if y_bounds is None:
            continue
        local_top, local_bottom = y_bounds
        line_top = max(top, top + local_top - 1)
        line_bottom = min(bottom, top + local_bottom + 1)
        result.append(
            (
                (absolute_left, line_top, absolute_right, line_bottom),
                round(confidence, 4),
            )
        )
    return result


def _horizontal_line_count(
    mask: Image.Image, system_box: tuple[int, int, int, int]
) -> int:
    left, top, right, bottom = system_box
    width = right - left
    row_counts, _columns = _region_projections(mask, left, top, right, bottom)
    return sum(count >= width * 0.38 for count in row_counts)


def _empty_result(width: int, height: int, warnings: list[dict[str, str]]) -> dict:
    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "image": {"width": width, "height": height},
        "confidence": 0.0,
        "content_region": None,
        "systems": [],
        "system_candidates": [],
        "barline_candidates": [],
        "warnings": warnings,
    }


def _analyze_oriented_grayscale(
    grayscale: Image.Image,
    *,
    max_dimension: int,
) -> dict:
    original_width, original_height = grayscale.size
    warnings: list[dict[str, str]] = []
    if min(original_width, original_height) < 160:
        warnings.append(
            _warning(
                "low_resolution",
                "圖片解析度偏低，譜線與小節線候選可能不完整。",
            )
        )

    working = grayscale.copy()
    if max(working.size) > max_dimension:
        working.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    width, height = working.size

    # 以模糊後的局部背景減去原圖，只保留「比附近更暗」的細線。
    # 這會抵銷拍照的漸層陰影與紙張邊緣，比全圖固定亮度門檻更穩定。
    blur_radius = max(3.0, min(12.0, min(width, height) * 0.008))
    local_background = working.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    dark_detail = ImageChops.subtract(local_background, working)
    detail_histogram = dark_detail.histogram()
    noise_floor = _percentile_from_histogram(detail_histogram, 0.90)
    threshold = max(12, min(48, noise_floor + 7))
    mask = dark_detail.point(lambda value: 255 if value >= threshold else 0, mode="L")

    rows, columns, ink_pixels = _mask_projections(mask)
    pixel_count = width * height
    ink_fraction = ink_pixels / max(1, pixel_count)
    if ink_fraction < 0.00045:
        warnings.append(
            _warning(
                "insufficient_ink",
                "未偵測到足以分析的譜線或符號，本頁不產生版面候選。",
            )
        )
        return _empty_result(original_width, original_height, warnings)
    if ink_fraction > 0.22:
        warnings.append(
            _warning(
                "dense_texture",
                "頁面含有大量紋理、陰影或深色區域，版面候選已降低信心值。",
            )
        )

    x_bounds = _weighted_bounds(columns)
    y_bounds = _weighted_bounds(rows)
    if x_bounds is None or y_bounds is None:
        warnings.append(
            _warning(
                "no_content_region",
                "無法穩定估計譜面內容區域。",
            )
        )
        return _empty_result(original_width, original_height, warnings)

    horizontal_padding = max(2, round(width * 0.012))
    vertical_padding = max(2, round(height * 0.012))
    content_box = (
        max(0, x_bounds[0] - horizontal_padding),
        max(0, y_bounds[0] - vertical_padding),
        min(width, x_bounds[1] + horizontal_padding),
        min(height, y_bounds[1] + vertical_padding),
    )
    content_strength = _clamp((ink_fraction - 0.00045) / 0.018)
    content_confidence = 0.55 + 0.36 * content_strength
    if ink_fraction > 0.22:
        content_confidence -= 0.18
    content_region = {
        "bbox": _normalized_bbox(*content_box, width, height),
        "confidence": round(_clamp(content_confidence, 0.3, 0.94), 4),
    }

    raw_systems = _candidate_system_ranges(mask, content_box)
    system_candidates: list[dict] = []
    barline_candidates: list[dict] = []
    systems: list[dict] = []
    next_measure_number = 1
    for raw_system in raw_systems:
        raw_barlines = _barline_ranges(mask, raw_system)
        horizontal_lines = _horizontal_line_count(mask, raw_system)
        system_width_ratio = (raw_system[2] - raw_system[0]) / max(
            1, content_box[2] - content_box[0]
        )
        # 兩條以上對齊的垂直線是最強的譜面證據。若小節線不清楚，
        # 也只在具有多條長水平譜線時保留系統候選。
        has_barline_evidence = len(raw_barlines) >= 2
        has_staff_evidence = horizontal_lines >= 4 and system_width_ratio >= 0.55
        if not (has_barline_evidence or has_staff_evidence):
            continue

        system_id = f"SYS-{len(system_candidates) + 1:03d}"
        barline_ids: list[str] = []
        nested_barlines: list[dict] = []
        for raw_bbox, confidence in raw_barlines:
            barline_id = f"BAR-{len(barline_candidates) + 1:04d}"
            barline_ids.append(barline_id)
            normalized_line = _normalized_bbox(*raw_bbox, width, height)
            record = {
                "candidate_id": barline_id,
                "system_candidate_id": system_id,
                "bbox": normalized_line,
                "confidence": confidence,
            }
            barline_candidates.append(record)
            nested_barlines.append(
                {
                    "barline_id": barline_id,
                    "bbox": dict(normalized_line),
                    "confidence": confidence,
                }
            )
        evidence_score = max(
            _clamp(len(raw_barlines) / 5.0),
            _clamp(horizontal_lines / 8.0),
        )
        system_confidence = 0.46 + 0.27 * evidence_score + 0.18 * _clamp(system_width_ratio)
        if ink_fraction > 0.22:
            system_confidence -= 0.14
        normalized_system = _normalized_bbox(*raw_system, width, height)
        normalized_system_confidence = round(
            _clamp(system_confidence, 0.4, 0.95), 4
        )
        system_candidates.append(
            {
                "candidate_id": system_id,
                "bbox": normalized_system,
                "confidence": normalized_system_confidence,
                "barline_candidate_ids": barline_ids,
            }
        )

        measure_regions: list[dict] = []
        # raw_barlines 由左到右產生。只有兩個相鄰邊界間有正寬度時，
        # 才建立小節區域；不根據頁面寬度猜測缺失的第一／最後小節。
        for measure_index, (
            (left_line, left_confidence),
            (right_line, right_confidence),
        ) in enumerate(zip(raw_barlines, raw_barlines[1:])):
            measure_left = round((left_line[0] + left_line[2]) / 2)
            measure_right = round((right_line[0] + right_line[2]) / 2)
            if measure_right - measure_left < max(3, round((raw_system[2] - raw_system[0]) * 0.015)):
                continue
            measure_id = f"MEASURE-{next_measure_number:04d}"
            next_measure_number += 1
            measure_regions.append(
                {
                    "measure_id": measure_id,
                    "bbox": _normalized_bbox(
                        measure_left,
                        raw_system[1],
                        measure_right,
                        raw_system[3],
                        width,
                        height,
                    ),
                    "confidence": round(
                        _clamp(
                            min(
                                normalized_system_confidence,
                                left_confidence,
                                right_confidence,
                            )
                            * 0.92
                        ),
                        4,
                    ),
                    "left_barline_id": nested_barlines[measure_index]["barline_id"],
                    "right_barline_id": nested_barlines[measure_index + 1]["barline_id"],
                }
            )
        systems.append(
            {
                "system_id": system_id,
                "bbox": dict(normalized_system),
                "confidence": normalized_system_confidence,
                "barlines": nested_barlines,
                "measure_regions": measure_regions,
            }
        )

    if not system_candidates:
        warnings.append(
            _warning(
                "no_score_systems",
                "偵測到內容，但沒有足夠證據建立橫向譜面系統候選；請人工確認。",
            )
        )
    elif len(barline_candidates) < 2 * len(system_candidates):
        warnings.append(
            _warning(
                "few_barlines",
                "部分譜面系統的小節線證據不足，分小節前需人工確認。",
            )
        )

    if systems:
        mean_system_confidence = sum(item["confidence"] for item in systems) / len(systems)
        page_confidence = round(
            _clamp(content_region["confidence"] * mean_system_confidence), 4
        )
    else:
        page_confidence = round(content_region["confidence"] * 0.25, 4)

    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "image": {"width": original_width, "height": original_height},
        "confidence": page_confidence,
        "content_region": content_region,
        "systems": systems,
        "system_candidates": system_candidates,
        "barline_candidates": barline_candidates,
        "warnings": warnings,
    }


def analyze_score_page(
    image_source: str | Path | Image.Image,
    *,
    max_dimension: int = 1400,
) -> dict:
    """分析一張已匯入的 JPEG/PNG 路徑或 Pillow Image。

    回傳值只包含 ``dict``/``list``/數字/文字/``None``，可直接寫入
    ``project.json``。無法確認時會回傳空候選與 warnings，不會猜測小節數。
    """

    if not isinstance(max_dimension, int) or isinstance(max_dimension, bool) or max_dimension < 256:
        raise ValueError("max_dimension 必須是不小於 256 的整數")
    if isinstance(image_source, Image.Image):
        try:
            # copy 確保 EXIF 方向處理與 convert 不會改動呼叫端的圖片。
            oriented = ImageOps.exif_transpose(image_source.copy()).convert("L")
        except OSError as error:
            raise ScoreLayoutError("無法解讀樂譜圖片") from error
    else:
        source = Path(image_source)
        if not source.is_file():
            raise FileNotFoundError(source)
        try:
            with Image.open(source) as opened:
                if opened.format not in SUPPORTED_IMAGE_FORMATS:
                    raise ScoreLayoutError("版面分析只支援 JPEG 與 PNG")
                oriented = ImageOps.exif_transpose(opened).convert("L")
        except ScoreLayoutError:
            raise
        except (OSError, UnidentifiedImageError) as error:
            raise ScoreLayoutError("無法解讀樂譜圖片") from error
    if oriented.width < 1 or oriented.height < 1:
        raise ScoreLayoutError("圖片尺寸無效")
    return _analyze_oriented_grayscale(oriented, max_dimension=max_dimension)


__all__ = [
    "COORDINATE_SPACE",
    "LAYOUT_SCHEMA_VERSION",
    "ScoreLayoutError",
    "analyze_score_page",
]
