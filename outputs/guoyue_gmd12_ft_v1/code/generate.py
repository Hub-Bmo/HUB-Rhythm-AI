"""Generate legal HIT_SET streams, MIDI and a simple audition WAV."""
import argparse
from pathlib import Path
import json
import wave

import numpy as np
import torch

from .model import ModelConfig, RhythmTransformer


STROKE_NOTES = {"kick": 36, "snare": 38, "side_stick": 37,
    "hihat_closed": 42, "hihat_open": 46, "hihat_pedal": 44,
    "tom_high": 48, "tom_mid": 45, "tom_low": 43, "ride": 51, "ride_bell": 53,
    "crash": 49, "splash": 55}


def allowed_tokens(vocab, phase, *, first=False, hit_count=0):
    if phase == "time":
        return [t for t in vocab if t.startswith("<TIME_SHIFT_") and (first or int(t[12:-1]) > 0)]
    if phase == "open":
        return ["<HIT_SET>"]
    if phase == "stroke":
        return [t for t in vocab if t.startswith("<STROKE_")]
    if phase == "velocity":
        return [t for t in vocab if t.startswith("<VEL_")]
    if phase == "next":
        strokes = [t for t in vocab if t.startswith("<STROKE_")]
        return (["<END_HIT_SET>"] + strokes) if hit_count < 16 else ["<END_HIT_SET>"]
    raise ValueError(phase)


@torch.inference_mode()
def generate_tokens(model, vocab, *, bars=8, bpm=138, temperature=0.85, top_k=12, seed=123, device="cpu", max_tokens=12000):
    if bars < 1 or temperature <= 0 or top_k < 1:
        raise ValueError("bars, temperature and top_k must be positive")
    torch.manual_seed(seed)
    model.eval()
    prefix = ["<BOS>", "<SOURCE_GMD>", "<TS_4-4>", f"<BPM_{bpm}>"]
    if any(t not in vocab for t in prefix):
        raise ValueError("Prompt metadata is not in this model vocabulary; choose a known BPM")
    output = list(prefix)
    phase, tick, hits, first = "time", 0, 0, True
    while len(output) < max_tokens:
        choices = allowed_tokens(vocab, phase, first=first, hit_count=hits)
        if not choices:
            raise ValueError("Checkpoint vocabulary has no legal continuation")
        if len(choices) == 1:
            token = choices[0]
        else:
            ids = torch.tensor([[vocab[t] for t in output[-model.config.context_length:]]], dtype=torch.long, device=device)
            logits = model(ids)[0, -1].float() / temperature
            candidates = torch.tensor([vocab[t] for t in choices], device=device)
            values = logits[candidates]
            top_values, top_indexes = torch.topk(values, min(top_k, len(choices)))
            chosen = int(top_indexes[torch.multinomial(torch.softmax(top_values, dim=-1), 1)])
            token = choices[chosen]
        output.append(token)
        if phase == "time":
            tick += int(token[12:-1])
            phase = "open"
        elif phase == "open":
            phase, hits = "stroke", 0
        elif phase == "stroke":
            phase = "velocity"
        elif phase == "velocity":
            phase, hits = "next", hits + 1
        elif token == "<END_HIT_SET>":
            phase, first = "time", False
            if tick >= bars * 4 * 960:
                output.append("<EOS>")
                return output
        else:
            phase = "velocity"
    raise RuntimeError("Generation token limit reached before the requested length; no incomplete sample exported")


def parse_events(tokens):
    tick, opened, pending, events = 0, False, None, []
    for token in tokens:
        if token.startswith("<TIME_SHIFT_"):
            if opened:
                raise ValueError("TIME_SHIFT inside HIT_SET")
            tick += int(token[12:-1])
        elif token == "<HIT_SET>":
            if opened:
                raise ValueError("Nested HIT_SET")
            opened = True
        elif token.startswith("<STROKE_"):
            if not opened or pending is not None:
                raise ValueError("Stroke missing group or velocity")
            pending = token[8:-1]
        elif token.startswith("<VEL_"):
            if pending is None:
                raise ValueError("Velocity without stroke")
            velocity = int(token[5:-1])
            if not 1 <= velocity <= 8:
                raise ValueError("Velocity outside source bins")
            events.append({"tick": tick, "stroke": pending, "velocity": min(127, velocity * 16)})
            pending = None
        elif token == "<END_HIT_SET>":
            if not opened or pending is not None:
                raise ValueError("Incomplete HIT_SET")
            opened = False
    if opened or pending is not None:
        raise ValueError("Incomplete sequence")
    return events


def note_for(stroke):
    if stroke in STROKE_NOTES:
        return STROKE_NOTES[stroke]
    if stroke.startswith("midi_note_") and 0 <= int(stroke[10:]) <= 127:
        return int(stroke[10:])
    raise ValueError(f"No MIDI rendering for stroke {stroke}")


def write_midi(path, events, bpm, end_tick):
    import mido
    midi = mido.MidiFile(ticks_per_beat=960)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    messages = []
    for event in events:
        if event["tick"] >= end_tick:
            continue
        note = note_for(event["stroke"])
        messages.append((event["tick"], 1, mido.Message("note_on", channel=9, note=note, velocity=event["velocity"])))
        messages.append((min(end_tick, event["tick"] + 60), 0, mido.Message("note_off", channel=9, note=note, velocity=0)))
    previous = 0
    for tick, priority, message in sorted(messages, key=lambda item: (item[0], item[1])):
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, end_tick - previous)))
    midi.save(path)


def write_wav(path, events, bpm, end_tick, sample_rate=44100):
    duration = end_tick / 960 * 60 / bpm
    audio = np.zeros(int((duration + 1) * sample_rate), dtype=np.float32)
    rng = np.random.default_rng(123)
    for event in events:
        if event["tick"] >= end_tick:
            continue
        stroke = event["stroke"]
        cymbal = any(x in stroke for x in ("hihat", "crash", "splash", "ride", "midi_note"))
        length = 0.45 if cymbal else 0.28
        t = np.arange(int(length * sample_rate), dtype=np.float32) / sample_rate
        noise = rng.normal(0, 1, len(t)).astype(np.float32)
        if stroke == "kick":
            sound = np.sin(2 * np.pi * (48 * t + 32 * 0.018 * (1 - np.exp(-t / 0.018)))) * np.exp(-t * 18)
        elif stroke in {"snare", "side_stick"}:
            sound = (0.65 * noise + 0.35 * np.sin(2 * np.pi * 180 * t)) * np.exp(-t * 27)
        elif cymbal:
            sound = np.diff(noise, prepend=noise[0]) * np.exp(-t * (30 if "closed" in stroke or "pedal" in stroke else 9)) * 0.27
        else:
            frequency = {"tom_high": 190, "tom_mid": 140, "tom_low": 95}.get(stroke, 220)
            sound = (np.sin(2 * np.pi * frequency * t) + 0.08 * noise) * np.exp(-t * 16)
        offset = round(event["tick"] / 960 * 60 / bpm * sample_rate)
        audio[offset:offset + len(sound)] += sound * event["velocity"] / 127 * 0.32
    peak = float(np.max(np.abs(audio)))
    if peak > 0.95:
        audio *= 0.95 / peak
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes((audio * 32767).astype("<i2").tobytes())


def generate_files(model, vocab, output_prefix, *, bars=8, bpm=138, seed=123, device="cpu", temperature=0.85, top_k=12):
    output_prefix = Path(output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    tokens = generate_tokens(model, vocab, bars=bars, bpm=bpm, seed=seed, device=device, temperature=temperature, top_k=top_k)
    events = parse_events(tokens)
    data = {"tokens": tokens, "events": events, "ppq": 960, "bpm": bpm, "bars": bars,
            "seed": seed, "temperature": temperature, "top_k": top_k,
            "note": "Grammar-constrained sampling; WAV uses simple synthetic audition sounds, not recorded drum samples."}
    output_prefix.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    output_prefix.with_suffix(".txt").write_text("\n".join(tokens) + "\n", encoding="utf-8")
    write_midi(output_prefix.with_suffix(".mid"), events, bpm, bars * 4 * 960)
    write_wav(output_prefix.with_suffix(".wav"), events, bpm, bars * 4 * 960)
    return data


def main():
    p = argparse.ArgumentParser(description="Generate continuous GMD Transformer rhythm")
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bars", type=int, default=8)
    p.add_argument("--bpm", type=int, default=138)
    p.add_argument("--seed", type=int, default=123)
    p.add_argument("--temperature", type=float, default=0.85)
    p.add_argument("--top-k", type=int, default=12)
    p.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = p.parse_args()
    torch.set_num_threads(4)
    checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    if checkpoint.get("architecture") != "gmd-causal-transformer-v1":
        raise ValueError("Expected a GMD Transformer checkpoint")
    model = RhythmTransformer(ModelConfig(**checkpoint["model_config"])).to(args.device)
    model.load_state_dict(checkpoint["model_state_dict"])
    data = generate_files(model, checkpoint["vocab"], args.output, bars=args.bars, bpm=args.bpm,
        seed=args.seed, device=args.device, temperature=args.temperature, top_k=args.top_k)
    print(f"Generated {args.bars} bars / {len(data['tokens'])} tokens: {args.output}")


if __name__ == "__main__":
    main()
