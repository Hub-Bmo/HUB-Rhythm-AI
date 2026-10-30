"""不依賴外部音色檔的簡易 WAV 合成器。

這些聲音只用於辨認軌道、校對節奏，不模擬真實國樂器音色。產生的 WAV
會交給跨平台 pygame 音訊後端，在 Windows 與 macOS 使用相同播放流程。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import random
import sys
from typing import Mapping
import wave

from .timeline import (
    PPQ,
    QUANTIZATION_TICKS,
    tick_to_seconds,
    ticks_per_bar,
    ticks_per_beat,
)


SAMPLE_RATE = 22_050

# Playback modifiers use musical time, not milliseconds. Keeping these values
# in ticks makes them follow the segment tempo automatically.
DEFAULT_EVENT_DURATION_TICKS = PPQ // 4  # one 1/16-note grid cell
ORNAMENT_LEAD_TICKS = PPQ // 8  # a short 1/32-note grace-note lead
ROLL_INTERVAL_TICKS = PPQ // 8  # audible 1/32-note repeated attacks
MAX_ROLL_HITS = 4_096


@dataclass(frozen=True)
class ScheduledHit:
    """One audible attack derived from a stored event.

    ``tick`` is allowed to be negative for an ornament attached to an event at
    tick zero. Renderers can add a small pre-roll while the stored event keeps
    its exact score position.
    """

    tick: int
    gain: float
    accent: bool
    role: str  # ``ornament``, ``main`` or ``roll``


def _annotation_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def _positive_tick_value(value: object) -> int | None:
    """Return a positive integral tick value, or ``None`` for legacy blanks."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ticks = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return ticks if ticks > 0 else None


def event_duration_ticks(
    event: Mapping[str, object],
    *,
    fallback_duration_ticks: int | None = None,
) -> int:
    """Resolve an event duration in PPQ ticks.

    Old projects saved ``duration: null``. For those events, prefer the
    event's recorded quantization and otherwise use one 1/16-note grid cell.
    A caller that knows the current bar's grid may supply it explicitly.
    """
    duration = _positive_tick_value(event.get("duration"))
    if duration is not None:
        return duration
    fallback = _positive_tick_value(fallback_duration_ticks)
    if fallback is not None:
        return fallback
    quantization = QUANTIZATION_TICKS.get(str(event.get("quantization", "")))
    return quantization or DEFAULT_EVENT_DURATION_TICKS


def schedule_event_hits(
    event: Mapping[str, object],
    *,
    fallback_duration_ticks: int | None = None,
    ornament_lead_ticks: int = ORNAMENT_LEAD_TICKS,
    roll_interval_ticks: int = ROLL_INTERVAL_TICKS,
    max_roll_hits: int = MAX_ROLL_HITS,
) -> tuple[ScheduledHit, ...]:
    """Expand one stored event into deterministic playback attacks.

    This is deliberately a pure function so recognition/import code and tests
    can validate playback without opening an audio device. Duration is always
    interpreted as PPQ ticks. An accented roll accents its first attack only;
    an ornament remains a separate, quieter unaccented attack, so all three
    modifiers can be combined without changing the source event.
    """
    try:
        event_tick = int(event.get("tick", 0))
    except (TypeError, ValueError, OverflowError):
        event_tick = 0

    stroke = str(event.get("stroke", ""))
    accent = _annotation_flag(event.get("is_accent", False))
    ornament = _annotation_flag(event.get("is_ornament", False))
    legacy_roll = "roll" in stroke.lower()
    roll = _annotation_flag(event.get("is_roll", legacy_roll))

    hits: list[ScheduledHit] = []
    if ornament:
        lead = max(1, int(ornament_lead_ticks))
        hits.append(ScheduledHit(event_tick - lead, 0.34, False, "ornament"))

    if not roll:
        hits.append(ScheduledHit(event_tick, 1.0, accent, "main"))
        return tuple(hits)

    duration = event_duration_ticks(
        event, fallback_duration_ticks=fallback_duration_ticks
    )
    requested_interval = max(1, int(roll_interval_ticks))
    # A one-cell legacy roll should still contain at least two discernible
    # attacks whenever its duration has more than one tick.
    interval = min(requested_interval, max(1, duration // 2))
    hit_limit = max(1, int(max_roll_hits))
    offset = 0
    index = 0
    while offset < duration and index < hit_limit:
        hits.append(
            ScheduledHit(
                event_tick + offset,
                0.90 if index == 0 else 0.78,
                accent if index == 0 else False,
                "roll",
            )
        )
        index += 1
        offset += interval
    return tuple(hits)


def _stable_seed(*values: str) -> int:
    digest = hashlib.sha256("|".join(values).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


def _sound_duration(profile: str, stroke: str, *, roll: bool = False) -> float:
    base = {
        "drum": 0.22,
        "large_gong": 1.35,
        "small_gong": 0.72,
        "cymbal": 1.05,
        "bell": 0.55,
        "timpani": 0.78,
        "metronome": 0.07,
    }.get(profile, 0.45)
    if "muted" in stroke:
        return min(base, 0.18)
    if roll or "roll" in stroke:
        return max(base, 0.9)
    return base


def synthesize_sound(
    profile: str,
    stroke: str,
    accent: bool = False,
    roll: bool = False,
) -> array:
    """產生一個短音色，傳回 -1..1 的浮點 samples。"""
    is_roll = roll or "roll" in stroke
    duration = _sound_duration(profile, stroke, roll=is_roll)
    count = max(1, int(duration * SAMPLE_RATE))
    result = array("f", [0.0]) * count
    rng = random.Random(_stable_seed(profile, stroke, "roll" if is_roll else "single"))
    amplitude = 1.0 if accent or "accent" in stroke else 0.72
    if "light" in stroke:
        amplitude *= 0.58

    previous_noise = 0.0
    for index in range(count):
        t = index / SAMPLE_RATE
        progress = index / count
        if profile == "metronome":
            frequency = 1_650 if accent else 1_150
            value = math.sin(2 * math.pi * frequency * t) * math.exp(-55 * t)
        elif profile == "drum":
            pitch_number = 0
            if stroke.startswith("drum_"):
                try:
                    pitch_number = max(0, int(stroke.rsplit("_", 1)[1]) - 1)
                except ValueError:
                    pitch_number = 0
            frequency = 145 + pitch_number * 23 - 80 * progress
            tone = math.sin(2 * math.pi * frequency * t)
            noise = rng.uniform(-1, 1)
            value = (0.86 * tone + 0.14 * noise) * math.exp(-17 * t)
        elif profile == "timpani":
            pitch_number = 0
            if stroke.startswith("pitch_"):
                try:
                    pitch_number = max(0, int(stroke.rsplit("_", 1)[1]) - 1)
                except ValueError:
                    pitch_number = 0
            frequency = 90 + pitch_number * 18
            sweep = frequency * (1.0 - 0.10 * progress)
            value = (
                0.78 * math.sin(2 * math.pi * sweep * t)
                + 0.22 * math.sin(2 * math.pi * sweep * 1.52 * t)
            ) * math.exp(-5.4 * t)
        elif profile == "large_gong":
            value = (
                0.65 * math.sin(2 * math.pi * 185 * t)
                + 0.25 * math.sin(2 * math.pi * 287 * t)
                + 0.18 * math.sin(2 * math.pi * 421 * t)
            ) * math.exp(-2.6 * t)
        elif profile == "small_gong":
            value = (
                0.72 * math.sin(2 * math.pi * 510 * t)
                + 0.30 * math.sin(2 * math.pi * 773 * t)
            ) * math.exp(-5.0 * t)
        elif profile == "cymbal":
            noise = rng.uniform(-1, 1)
            high_noise = noise - previous_noise * 0.7
            previous_noise = noise
            value = high_noise * math.exp(-4.0 * t)
        else:  # bell / fallback
            value = (
                0.72 * math.sin(2 * math.pi * 930 * t)
                + 0.25 * math.sin(2 * math.pi * 1_470 * t)
            ) * math.exp(-7.0 * t)

        if is_roll:
            value *= 0.55 + 0.45 * (math.sin(2 * math.pi * 13 * t) ** 2)
        result[index] = max(-1.0, min(1.0, value * amplitude))
    return result


def _mix(buffer: array, sound: array, start_seconds: float, gain: float = 1.0) -> None:
    raw_start = int(round(start_seconds * SAMPLE_RATE))
    start = max(0, raw_start)
    source_start = max(0, -raw_start)
    available = min(len(sound) - source_start, len(buffer) - start)
    for index in range(max(0, available)):
        buffer[start + index] += sound[source_start + index] * gain


def _shortened_sound(sound: array, max_duration: float) -> array:
    """Return a short, faded copy suitable for ornaments or roll attacks."""
    count = min(len(sound), max(1, int(max_duration * SAMPLE_RATE)))
    fade_samples = min(count, max(1, int(SAMPLE_RATE * 0.018)))
    result = array("f", sound[:count])
    for index in range(count - fade_samples, count):
        result[index] *= (count - index - 1) / fade_samples
    return result


def render_segment_wav(
    path: str | Path,
    segment: dict,
    tracks: list[dict],
    *,
    count_in_bars: int = 0,
    include_metronome: bool = True,
    start_tick: int = 0,
    end_tick: int | None = None,
) -> Path:
    """將指定軌道目前選取的 Take 合成為單聲道 WAV。"""
    from .models import selected_take
    from .grid import (
        bar_start_ticks,
        ensure_bar_settings,
        grid_ticks,
        segment_total_ticks,
    )

    tempo = float(segment["tempo"])
    settings = ensure_bar_settings(segment)
    signature = settings[0]["time_signature"]
    count_in_ticks = count_in_bars * ticks_per_bar(signature)
    whole_end = segment_total_ticks(segment)
    start_tick = max(0, int(start_tick))
    end_tick = whole_end if end_tick is None else min(whole_end, max(start_tick, int(end_tick)))
    starts = bar_start_ticks(segment)

    def fallback_duration_at(event_tick: int) -> int:
        bar_index = 0
        for index, bar_start in enumerate(starts):
            if bar_start > event_tick:
                break
            bar_index = index
        return grid_ticks(settings[bar_index]["subdivision"])

    render_hits: list[tuple[str, str, ScheduledHit]] = []
    for track in tracks:
        take = selected_take(track)
        if take is None:
            continue
        profile = str(track.get("sound_profile", "bell"))
        for event in take.get("events", []):
            try:
                event_tick = int(event.get("tick", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            hits = schedule_event_hits(
                event,
                fallback_duration_ticks=fallback_duration_at(event_tick),
            )
            for hit in hits:
                # A grace note belongs to its main event, even if it needs a
                # short pre-roll before the selected range. Roll attacks are
                # filtered independently, which also lets a roll that starts
                # before the range continue into it.
                if hit.role == "ornament":
                    include = start_tick <= event_tick < end_tick
                else:
                    include = start_tick <= hit.tick < end_tick
                if include:
                    render_hits.append(
                        (profile, str(event.get("stroke", "open")), hit)
                    )

    earliest_tick = min(
        (hit.tick for _profile, _stroke, hit in render_hits),
        default=start_tick,
    )
    leading_padding_ticks = max(0, start_tick - earliest_tick - count_in_ticks)
    total_seconds = tick_to_seconds(
        leading_padding_ticks + count_in_ticks + end_tick - start_tick,
        tempo,
    ) + 1.6
    buffer = array("f", [0.0]) * int(total_seconds * SAMPLE_RATE)

    sound_cache: dict[tuple[str, ...], array] = {}
    if include_metronome:
        first_numerator = int(signature.split("/", 1)[0])
        first_beat_ticks = ticks_per_beat(signature)
        for beat_index in range(count_in_bars * first_numerator):
            accent = beat_index % first_numerator == 0
            key = ("metronome", "accent" if accent else "beat")
            sound_cache.setdefault(
                key, synthesize_sound("metronome", key[1], accent=accent)
            )
            beat_time = tick_to_seconds(
                leading_padding_ticks + beat_index * first_beat_ticks,
                tempo,
            )
            _mix(buffer, sound_cache[key], beat_time, gain=0.56)

        current_tick = 0
        for setting in settings:
            numerator = int(setting["time_signature"].split("/", 1)[0])
            beat_ticks = ticks_per_beat(setting["time_signature"])
            bar_end = current_tick + ticks_per_bar(setting["time_signature"])
            if bar_end > start_tick and current_tick < end_tick:
                for beat_index in range(numerator):
                    beat_tick = current_tick + beat_index * beat_ticks
                    if start_tick <= beat_tick < end_tick:
                        accent = beat_index == 0
                        key = ("metronome", "accent" if accent else "beat")
                        sound_cache.setdefault(
                            key, synthesize_sound("metronome", key[1], accent=accent)
                        )
                        beat_time = tick_to_seconds(
                            leading_padding_ticks
                            + count_in_ticks
                            + beat_tick
                            - start_tick,
                            tempo,
                        )
                        _mix(buffer, sound_cache[key], beat_time, gain=0.56)
            current_tick = bar_end

    roll_hit_duration = max(
        0.06,
        min(0.18, tick_to_seconds(ROLL_INTERVAL_TICKS, tempo) * 1.4),
    )
    for profile, stroke, hit in render_hits:
        key = (
            profile,
            stroke,
            "accent" if hit.accent else "normal",
            hit.role,
        )
        if key not in sound_cache:
            sound = synthesize_sound(
                profile,
                stroke,
                accent=hit.accent,
                roll=False,
            )
            if hit.role == "ornament":
                sound = _shortened_sound(sound, 0.10)
            elif hit.role == "roll":
                sound = _shortened_sound(sound, roll_hit_duration)
            sound_cache[key] = sound
        start = tick_to_seconds(
            leading_padding_ticks
            + count_in_ticks
            + hit.tick
            - start_tick,
            tempo,
        )
        _mix(buffer, sound_cache[key], start, gain=0.62 * hit.gain)

    peak = max((abs(value) for value in buffer), default=1.0)
    scale = 0.92 / max(1.0, peak)
    pcm = array("h", (
        int(max(-1.0, min(1.0, value * scale)) * 32_767) for value in buffer
    ))
    if sys.byteorder != "little":
        pcm.byteswap()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return path


def play_wav(path: str | Path, *, loop: bool = False) -> None:
    from .audio_backend import get_audio_backend

    get_audio_backend().play_file(path, loop=loop)


def stop_audio() -> None:
    from .audio_backend import get_audio_backend

    get_audio_backend().stop_all()


def shutdown_audio() -> None:
    """釋放 SDL 音訊裝置，讓封裝版能乾淨結束。"""
    from .audio_backend import get_audio_backend

    get_audio_backend().shutdown()
