"""離線 16-bit WAV 合成與 Windows 播放；無第三方音訊套件、無網路。"""
from __future__ import annotations

from array import array
from hashlib import sha256
import math
from pathlib import Path
import random
import sys
import wave

from guoyue_annotator.photo_assets import safe_project_path
from score_recognizer.contract import ContractError
from score_recognizer.workspace import atomic_bytes

SAMPLE_RATE = 22050


def synthesized(profile, midi=None, duration=.4):
    """簡單的校對音效，音高採實際 MIDI；不宣稱為真實國樂採樣。"""
    rng = random.Random(int.from_bytes(sha256(f"{profile}:{midi}".encode()).digest()[:4], "little"))
    frequency = 440 * 2 ** (((midi if midi is not None else {"drum": 42, "timpani": 43, "bell": 76}.get(profile, 60)) - 69) / 12)
    seconds = .07 if profile == "metronome" else min(max(duration, .08), 2.5)
    samples = array("f")
    for index in range(int(SAMPLE_RATE * seconds)):
        t = index / SAMPLE_RATE
        if profile == "metronome":
            value = math.sin(2 * math.pi * 1450 * t) * math.exp(-t * 70)
        elif profile in {"drum", "timpani"}:
            decay = 9 if profile == "drum" else 3
            value = (math.sin(2 * math.pi * frequency * t) + .22 * math.sin(2 * math.pi * frequency * 1.51 * t)) * math.exp(-t * decay)
            value += rng.uniform(-.4, .4) * math.exp(-t * 60)
        elif profile == "cymbal":
            value = rng.uniform(-1, 1) * math.exp(-t * 4)
        elif profile in {"large_gong", "small_gong"}:
            base = 105 if profile == "large_gong" else 370
            value = sum(math.sin(2 * math.pi * base * ratio * t) * weight for ratio, weight in ((1, .55), (1.46, .3), (2.17, .15))) * math.exp(-t * 2.5)
        else:
            value = (math.sin(2 * math.pi * frequency * t) + .3 * math.sin(2 * math.pi * frequency * 2.76 * t)) * math.exp(-t * 4)
        release = min(1, max(0, seconds - t) / .015)
        samples.append(value * min(1, t * 1500) * release * .5)
    return samples


def read_sample(path):
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() not in {1, 2} or stream.getcomptype() != "NONE":
            raise ContractError("音效必須是單／雙聲道 16-bit PCM WAV")
        channels, rate, count = stream.getnchannels(), stream.getframerate(), stream.getnframes()
        if count / rate > 15:
            raise ContractError("單次音效超過 15 秒")
        raw = array("h", stream.readframes(count))
    if sys.byteorder != "little":
        raw.byteswap()
    mono = [sum(raw[i:i + channels]) / (channels * 32768) for i in range(0, len(raw), channels)]
    result = array("f")
    for index in range(round(len(mono) * SAMPLE_RATE / rate)):
        position = index * rate / SAMPLE_RATE
        first = min(int(position), len(mono) - 1)
        last = min(first + 1, len(mono) - 1)
        result.append(mono[first] + (mono[last] - mono[first]) * (position - first))
    return result


def render_wav(plan, root, target, *, cancel=None, roll_hz=20):
    if not 0 <= plan["duration"] <= 600:
        raise ContractError("音訊長度超出 10 分鐘")
    # 低音量疊音 + 全段一次性 peak normalization，避免多樂器相撞時削波。
    mix = array("f", [0]) * max(1, round((plan["duration"] + 2.6) * SAMPLE_RATE))
    cache = {}
    dynamics = {"ppp": .22, "pp": .3, "p": .42, "mp": .58, "mf": .72, "f": .86, "ff": 1, "fff": 1}
    for note in plan["notes"]:
        if cancel is not None and cancel.is_set():
            return None
        starts = [(note["start"], 1.0)]
        if note["is_roll"]:
            count = min(12000, max(1, math.ceil(note["duration"] * roll_hz)))
            starts = [(note["start"] + index / roll_hz, 1 if index == 0 else .75) for index in range(count)
                      if index / roll_hz < note["duration"]]
        if note["is_ornament"]:
            starts.insert(0, (max(0, note["start"] - note["beat_seconds"] / 8), .35))
        for midi in note["pitches"] or [None]:
            sample_path = safe_project_path(root, note["sample"], must_exist=True) if note.get("sample") else None
            # 使用者樣本是單次敲擊，不依 MIDI 改調；合成音則使用真實音高。
            # 輪奏用短音反覆起音，避免每次都疊上一整個長音而蓋住下一顆音。
            length = .12 if note["is_roll"] else min(2.5, max(.25, note["duration"]))
            key = (str(sample_path), note["profile"], midi, round(length, 3))
            if key not in cache:
                cache[key] = read_sample(sample_path) if sample_path else synthesized(note["profile"], midi, length)
            tone = cache[key]
            for attack, scale in starts:
                if cancel is not None and cancel.is_set():
                    return None
                offset = round(attack * SAMPLE_RATE)
                gain = .45 * dynamics.get(note.get("dynamic"), .72) * scale
                if note["is_accent"] and abs(attack - note["start"]) < 1e-6:
                    gain *= 1.35
                for index in range(min(len(tone), len(mix) - offset)):
                    mix[offset + index] += tone[index] * gain
    peak = max((abs(value) for value in mix), default=0)
    factor = min(1, .95 / peak) if peak else 1
    pcm = array("h", (round(max(-1, min(1, value * factor)) * 32767) for value in mix))
    if sys.byteorder != "little":
        pcm.byteswap()
    import io
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(pcm.tobytes())
    if cancel is not None and cancel.is_set():
        return None
    atomic_bytes(Path(target), buffer.getvalue())
    return Path(target)


class Player:
    def play(self, path):
        if sys.platform != "win32":
            raise ContractError("此版內建播放用於 Windows；可匯出 WAV 後以系統播放器開啟")
        import winsound
        winsound.PlaySound(str(Path(path).resolve()), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def stop(self):
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(None, 0)
