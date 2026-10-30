"""Windows 與 macOS 共用的即時擊打提示音。"""

from __future__ import annotations

from array import array
import sys
import threading

from .audio import (
    DEFAULT_EVENT_DURATION_TICKS,
    SAMPLE_RATE,
    _mix,
    _shortened_sound,
    schedule_event_hits,
    synthesize_sound,
)
from .audio_backend import get_audio_backend
from .timeline import tick_to_seconds


def make_hit_pcm(
    profile: str,
    stroke: str,
    max_duration: float = 0.32,
    *,
    accent: bool = False,
    ornament: bool = False,
    roll: bool = False,
    duration_ticks: int | None = None,
    tempo: float = 120.0,
    fallback_duration_ticks: int = DEFAULT_EVENT_DURATION_TICKS,
) -> bytes:
    """把一個事件的修飾音合成為適合即時回饋的 16-bit PCM。

    ``duration_ticks`` is a PPQ duration, matching persisted event data. The
    older call shape remains valid; omitted roll durations use one short grid.
    """
    try:
        playback_tempo = float(tempo)
    except (TypeError, ValueError):
        playback_tempo = 120.0
    if playback_tempo <= 0:
        playback_tempo = 120.0

    event = {
        "tick": 0,
        "stroke": stroke,
        "is_accent": accent,
        "is_ornament": ornament,
        "is_roll": roll,
        "duration": duration_ticks,
    }
    hits = schedule_event_hits(
        event,
        fallback_duration_ticks=fallback_duration_ticks,
    )
    first_tick = min((hit.tick for hit in hits), default=0)
    origin_tick = max(0, -first_tick)

    scheduled_sounds: list[tuple[float, array, float]] = []
    required_samples = 1
    for hit in hits:
        sound = synthesize_sound(
            profile,
            stroke,
            accent=hit.accent,
            roll=False,
        )
        if hit.role == "ornament":
            sound = _shortened_sound(sound, 0.10)
        start_seconds = tick_to_seconds(origin_tick + hit.tick, playback_tempo)
        required_samples = max(
            required_samples,
            int(round(start_seconds * SAMPLE_RATE)) + len(sound),
        )
        scheduled_sounds.append((start_seconds, sound, hit.gain))

    maximum_samples = max(1, int(SAMPLE_RATE * max(0.0, float(max_duration))))
    count = min(required_samples, maximum_samples)
    mixed = array("f", [0.0]) * count
    for start_seconds, sound, gain in scheduled_sounds:
        _mix(mixed, sound, start_seconds, gain=gain)

    fade_samples = min(count, int(SAMPLE_RATE * 0.018))
    pcm = array("h")
    for index in range(count):
        fade = 1.0
        if index >= count - fade_samples:
            fade = (count - index - 1) / max(1, fade_samples)
        value = max(-1.0, min(1.0, mixed[index] * 0.72 * fade))
        pcm.append(int(value * 32_767))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


class LiveHitPlayer:
    """快取 pygame Sound，讓多次擊打可以低延遲重疊播放。"""

    def __init__(self) -> None:
        self._backend = get_audio_backend()
        self.available = self._backend.available
        self._cache: dict[
            tuple[str, str, bool, bool, bool, int | None, float], object
        ] = {}
        self._cache_lock = threading.Lock()

    @property
    def error_message(self) -> str:
        return self._backend.error or "未知的音訊初始化錯誤"

    def _sound(
        self,
        profile: str,
        stroke: str,
        *,
        accent: bool = False,
        ornament: bool = False,
        roll: bool = False,
        duration_ticks: int | None = None,
        tempo: float = 120.0,
    ):
        key = (
            profile,
            stroke,
            accent,
            ornament,
            roll,
            duration_ticks,
            float(tempo),
        )
        with self._cache_lock:
            sound = self._cache.get(key)
        if sound is None and self.available:
            sound = self._backend.make_sound(
                make_hit_pcm(
                    profile,
                    stroke,
                    accent=accent,
                    ornament=ornament,
                    roll=roll,
                    duration_ticks=duration_ticks,
                    tempo=tempo,
                )
            )
            if sound is not None:
                with self._cache_lock:
                    self._cache[key] = sound
        return sound

    def prepare(self, profile: str, strokes: list[str]) -> None:
        if not self.available:
            return
        for stroke in strokes:
            self._sound(profile, stroke)

    def play(
        self,
        profile: str,
        stroke: str,
        *,
        accent: bool = False,
        ornament: bool = False,
        roll: bool = False,
        duration_ticks: int | None = None,
        tempo: float = 120.0,
    ) -> None:
        if self.available:
            self._backend.play_sound(
                self._sound(
                    profile,
                    stroke,
                    accent=accent,
                    ornament=ornament,
                    roll=roll,
                    duration_ticks=duration_ticks,
                    tempo=tempo,
                )
            )
