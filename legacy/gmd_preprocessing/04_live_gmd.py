from pathlib import Path
import json
import random
import threading
import queue
import time
import math

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import ttk

import torch
import torch.nn as nn


# ============================================================
# Settings
# ============================================================

MODEL_FILE = Path("gmd_pretrained.pt")

DEVICE = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)

PPQ = 960

DEFAULT_BPM = 138
DEFAULT_TEMPERATURE = 0.85

BARS_PER_CHUNK = 4

TOP_K = 12

SAMPLE_RATE = 44100


# ============================================================
# Load model
# ============================================================

checkpoint = torch.load(
    MODEL_FILE,
    map_location=DEVICE,
    weights_only=False
)

vocab = checkpoint["vocab"]

stoi = vocab

itos = {
    int(v): k
    for k, v in vocab.items()
}

VOCAB_SIZE = checkpoint["vocab_size"]

EMB_DIM = checkpoint["emb_dim"]
HIDDEN_DIM = checkpoint["hidden_dim"]
NUM_LAYERS = checkpoint["num_layers"]

PAD_ID = stoi["<PAD>"]


# ============================================================
# Model
# ============================================================

class LSTMTokenLM(nn.Module):

    def __init__(
        self,
        vocab_size,
        emb_dim,
        hidden_dim,
        num_layers
    ):

        super().__init__()

        self.embedding = nn.Embedding(
            vocab_size,
            emb_dim,
            padding_idx=PAD_ID
        )

        self.lstm = nn.LSTM(
            emb_dim,
            hidden_dim,
            num_layers=num_layers,
            batch_first=True
        )

        self.fc = nn.Linear(
            hidden_dim,
            vocab_size
        )

    def forward(
        self,
        x,
        hidden=None
    ):

        x = self.embedding(x)

        output, hidden = self.lstm(
            x,
            hidden
        )

        logits = self.fc(output)

        return logits, hidden


model = LSTMTokenLM(
    VOCAB_SIZE,
    EMB_DIM,
    HIDDEN_DIM,
    NUM_LAYERS
).to(DEVICE)

model.load_state_dict(
    checkpoint["model_state_dict"]
)

model.eval()


# ============================================================
# Token helpers
# ============================================================

def get_token_id(token):

    return stoi.get(
        token,
        stoi["<UNK>"]
    )


def time_shift_value(token):

    if token.startswith("<TIME_SHIFT_"):

        return int(
            token[
                12:-1
            ]
        )

    return None


def velocity_value(token):

    if token.startswith("<VEL_"):

        level = int(
            token[
                5:-1
            ]
        )

        return int(
            max(
                1,
                min(
                    127,
                    round(
                        level / 8 * 127
                    )
                )
            )
        )

    return None


def stroke_name(token):

    if token.startswith("<STROKE_"):

        return token[
            8:-1
        ]

    return None


# ============================================================
# Drum synthesis
# ============================================================

def make_kick(
    velocity,
    duration=0.25
):

    n = int(
        SAMPLE_RATE * duration
    )

    t = np.arange(n) / SAMPLE_RATE

    frequency = (
        120
        * np.exp(-t * 18)
        + 45
    )

    phase = (
        2
        * np.pi
        * np.cumsum(frequency)
        / SAMPLE_RATE
    )

    envelope = np.exp(
        -t * 14
    )

    sound = (
        np.sin(phase)
        * envelope
    )

    return sound * (
        velocity / 127
    )


def make_snare(
    velocity,
    duration=0.18
):

    n = int(
        SAMPLE_RATE * duration
    )

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(
        -t * 22
    )

    tone = (
        np.sin(
            2
            * np.pi
            * 180
            * t
        )
        * np.exp(-t * 30)
    )

    sound = (
        noise * 0.75
        + tone * 0.25
    )

    return (
        sound
        * envelope
        * (velocity / 127)
    )


def make_hihat(
    velocity,
    duration=0.07
):

    n = int(
        SAMPLE_RATE * duration
    )

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(
        -t * 65
    )

    sound = (
        noise
        * envelope
    )

    return (
        sound
        * (velocity / 127)
    )


def make_tom(
    velocity,
    frequency=140,
    duration=0.22
):

    n = int(
        SAMPLE_RATE * duration
    )

    t = np.arange(n) / SAMPLE_RATE

    envelope = np.exp(
        -t * 15
    )

    sound = (
        np.sin(
            2
            * np.pi
            * frequency
            * t
        )
        * envelope
    )

    return (
        sound
        * (velocity / 127)
    )


def make_cymbal(
    velocity,
    duration=0.35
):

    n = int(
        SAMPLE_RATE * duration
    )

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(
        -t * 7
    )

    sound = (
        noise
        * envelope
    )

    return (
        sound
        * (velocity / 127)
        * 0.5
    )


def drum_sound(
    stroke,
    velocity
):

    if stroke == "kick":
        return make_kick(
            velocity
        )

    if stroke == "snare":
        return make_snare(
            velocity
        )

    if stroke in (
        "hihat_closed",
        "hihat_pedal"
    ):
        return make_hihat(
            velocity
        )

    if stroke == "hihat_open":
        return make_hihat(
            velocity,
            duration=0.25
        )

    if stroke == "tom_low":
        return make_tom(
            velocity,
            frequency=100
        )

    if stroke == "tom_mid":
        return make_tom(
            velocity,
            frequency=140
        )

    if stroke == "tom_high":
        return make_tom(
            velocity,
            frequency=190
        )

    if stroke in (
        "crash",
        "ride",
        "ride_bell",
        "splash"
    ):
        return make_cymbal(
            velocity
        )

    return None


# ============================================================
# Allowed token grammar
# ============================================================

def candidates_for_state(state):

    result = []

    for token, idx in stoi.items():

        if token in (
            "<PAD>",
            "<UNK>",
            "<BOS>",
            "<SOURCE_GMD>",
            "<TS_4-4>",
            "<BPM_138>",
        ):
            continue

        if state == "TIME":

            if token.startswith(
                "<TIME_SHIFT_"
            ):
                result.append(idx)

        elif state == "HIT":

            if token == "<HIT_SET>":
                result.append(idx)

        elif state == "STROKE":

            if token.startswith(
                "<STROKE_"
            ):

                result.append(idx)

            elif token == "<END_HIT_SET>":

                result.append(idx)

        elif state == "VEL":

            if token.startswith(
                "<VEL_"
            ):

                result.append(idx)

    return result


# ============================================================
# Sampling
# ============================================================

def sample_token(
    logits,
    temperature,
    candidates
):

    if not candidates:
        return None

    filtered = torch.full_like(
        logits,
        float("-inf")
    )

    filtered[
        candidates
    ] = logits[
        candidates
    ]

    filtered = (
        filtered
        / temperature
    )

    values, indices = torch.topk(
        filtered,
        min(
            TOP_K,
            len(candidates)
        )
    )

    top = torch.full_like(
        filtered,
        float("-inf")
    )

    top[
        indices
    ] = values

    probabilities = torch.softmax(
        top,
        dim=-1
    )

    return torch.multinomial(
        probabilities,
        1
    ).item()


# ============================================================
# Generate one chunk
# ============================================================

def generate_chunk(
    temperature,
    stop_event
):

    prompt = [
        "<BOS>",
        "<SOURCE_GMD>",
        "<TS_4-4>",
        "<BPM_138>",
    ]

    ids = [
        get_token_id(x)
        for x in prompt
    ]

    tensor = torch.tensor(
        [ids],
        dtype=torch.long,
        device=DEVICE
    )

    with torch.no_grad():

        logits, hidden = model(
            tensor
        )

    tokens = []

    state = "TIME"

    elapsed_ticks = 0

    target_ticks = (
        BARS_PER_CHUNK
        * 4
        * PPQ
    )

    current_stroke = None

    current_velocity = 80

    events = []

    while (
        elapsed_ticks
        < target_ticks
    ):

        if stop_event.is_set():
            break

        candidates = (
            candidates_for_state(
                state
            )
        )

        next_id = sample_token(
            logits[0, -1],
            temperature,
            candidates
        )

        if next_id is None:
            break

        token = itos[
            next_id
        ]

        # ---------------------------------
        # TIME
        # ---------------------------------

        if state == "TIME":

            shift = time_shift_value(
                token
            )

            if shift is None:
                continue

            elapsed_ticks += shift

            state = "HIT"

        # ---------------------------------
        # HIT_SET
        # ---------------------------------

        elif state == "HIT":

            if token == "<HIT_SET>":

                state = "STROKE"

        # ---------------------------------
        # STROKE
        # ---------------------------------

        elif state == "STROKE":

            if token == "<END_HIT_SET>":

                state = "TIME"

            else:

                current_stroke = (
                    stroke_name(token)
                )

                state = "VEL"

        # ---------------------------------
        # VELOCITY
        # ---------------------------------

        elif state == "VEL":

            velocity = velocity_value(
                token
            )

            if velocity is not None:

                current_velocity = velocity

                events.append(
                    (
                        elapsed_ticks,
                        current_stroke,
                        current_velocity
                    )
                )

                state = "STROKE"

        # ---------------------------------
        # Feed next token
        # ---------------------------------

        tensor = torch.tensor(
            [[next_id]],
            dtype=torch.long,
            device=DEVICE
        )

        with torch.no_grad():

            logits, hidden = model(
                tensor,
                hidden
            )

    return events


# ============================================================
# Render audio
# ============================================================

def render_audio(
    events,
    bpm
):

    ticks_per_second = (
        PPQ
        * bpm
        / 60
    )

    duration_ticks = (
        BARS_PER_CHUNK
        * 4
        * PPQ
    )

    duration_seconds = (
        duration_ticks
        / ticks_per_second
    )

    audio_length = int(
        (
            duration_seconds
            + 1.0
        )
        * SAMPLE_RATE
    )

    audio = np.zeros(
        audio_length,
        dtype=np.float32
    )

    for (
        tick,
        stroke,
        velocity
    ) in events:

        sound = drum_sound(
            stroke,
            velocity
        )

        if sound is None:
            continue

        seconds = (
            tick
            / ticks_per_second
        )

        start = int(
            seconds
            * SAMPLE_RATE
        )

        end = min(
            start + len(sound),
            len(audio)
        )

        if start < end:

            audio[
                start:end
            ] += sound[
                :end-start
            ]

    # Prevent clipping
    peak = np.max(
        np.abs(audio)
    )

    if peak > 0.95:

        audio *= (
            0.95 / peak
        )

    return audio


# ============================================================
# GUI
# ============================================================

class LiveGenerator:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "GMD Live Groove Generator"
        )

        self.root.geometry(
            "650x500"
        )

        self.running = False

        self.stop_event = (
            threading.Event()
        )

        self.temperature = (
            DEFAULT_TEMPERATURE
        )

        self.bpm = DEFAULT_BPM

        self.bar_count = 0

        self.status_queue = (
            queue.Queue()
        )

        self.build_ui()

        self.root.after(
            100,
            self.update_ui
        )


    def build_ui(self):

        title = tk.Label(
            self.root,
            text="GMD Live Groove Generator",
            font=("Arial", 22, "bold")
        )

        title.pack(
            pady=20
        )


        control_frame = tk.Frame(
            self.root
        )

        control_frame.pack(
            pady=10
        )


        # BPM

        tk.Label(
            control_frame,
            text="BPM"
        ).grid(
            row=0,
            column=0,
            padx=10,
            pady=8
        )

        self.bpm_var = tk.IntVar(
            value=DEFAULT_BPM
        )

        tk.Spinbox(
            control_frame,
            from_=60,
            to=200,
            textvariable=self.bpm_var,
            width=8
        ).grid(
            row=0,
            column=1
        )


        # Temperature

        tk.Label(
            control_frame,
            text="Temperature"
        ).grid(
            row=1,
            column=0,
            padx=10,
            pady=8
        )

        self.temp_var = tk.DoubleVar(
            value=DEFAULT_TEMPERATURE
        )

        self.temp_scale = tk.Scale(
            control_frame,
            from_=0.5,
            to=1.3,
            resolution=0.05,
            orient="horizontal",
            variable=self.temp_var,
            length=250
        )

        self.temp_scale.grid(
            row=1,
            column=1
        )


        # Chunk size

        tk.Label(
            control_frame,
            text="Generation"
        ).grid(
            row=2,
            column=0,
            padx=10,
            pady=8
        )

        tk.Label(
            control_frame,
            text="4 bars / chunk"
        ).grid(
            row=2,
            column=1
        )


        # Status

        self.status_label = tk.Label(
            self.root,
            text="Ready",
            font=("Arial", 14)
        )

        self.status_label.pack(
            pady=20
        )


        self.bar_label = tk.Label(
            self.root,
            text="Bar: 0",
            font=("Arial", 18)
        )

        self.bar_label.pack(
            pady=10
        )


        # Buttons

        button_frame = tk.Frame(
            self.root
        )

        button_frame.pack(
            pady=20
        )


        self.start_button = tk.Button(
            button_frame,
            text="▶ START",
            font=("Arial", 16, "bold"),
            width=12,
            command=self.start
        )

        self.start_button.grid(
            row=0,
            column=0,
            padx=15
        )


        self.stop_button = tk.Button(
            button_frame,
            text="■ STOP",
            font=("Arial", 16, "bold"),
            width=12,
            command=self.stop,
            state="disabled"
        )

        self.stop_button.grid(
            row=0,
            column=1,
            padx=15
        )


        # Info

        self.info_label = tk.Label(
            self.root,
            text=(
                "Model: GMD LSTM\n"
                "Continuous generation: ON"
            ),
            justify="left"
        )

        self.info_label.pack(
            pady=20
        )


    def start(self):

        if self.running:
            return

        self.running = True

        self.stop_event.clear()

        self.bar_count = 0

        self.start_button.config(
            state="disabled"
        )

        self.stop_button.config(
            state="normal"
        )

        self.status_label.config(
            text="Generating..."
        )

        thread = threading.Thread(
            target=self.generation_loop,
            daemon=True
        )

        thread.start()


    def stop(self):

        self.stop_event.set()

        self.running = False

        self.status_label.config(
            text="Stopping..."
        )


    def generation_loop(self):

        while (
            not self.stop_event.is_set()
        ):

            try:

                bpm = int(
                    self.bpm_var.get()
                )

                temperature = float(
                    self.temp_var.get()
                )

                events = generate_chunk(
                    temperature,
                    self.stop_event
                )

                if (
                    self.stop_event.is_set()
                ):
                    break

                audio = render_audio(
                    events,
                    bpm
                )

                self.status_queue.put(
                    (
                        "chunk",
                        len(events)
                    )
                )

                sd.play(
                    audio,
                    SAMPLE_RATE,
                    blocking=True
                )

                self.bar_count += (
                    BARS_PER_CHUNK
                )

            except Exception as e:

                self.status_queue.put(
                    (
                        "error",
                        str(e)
                    )
                )

                break

        sd.stop()

        self.status_queue.put(
            (
                "stopped",
                None
            )
        )


    def update_ui(self):

        try:

            while True:

                message, value = (
                    self.status_queue.get_nowait()
                )

                if message == "chunk":

                    self.bar_label.config(
                        text=(
                            f"Bar: "
                            f"{self.bar_count}"
                        )
                    )

                    self.status_label.config(
                        text=(
                            f"Playing / "
                            f"Events: {value}"
                        )
                    )

                elif message == "error":

                    self.status_label.config(
                        text=(
                            "Error: "
                            + value
                        )
                    )

                    self.running = False

                elif message == "stopped":

                    self.running = False

                    self.start_button.config(
                        state="normal"
                    )

                    self.stop_button.config(
                        state="disabled"
                    )

                    if (
                        self.status_label[
                            "text"
                        ]
                        == "Stopping..."
                    ):

                        self.status_label.config(
                            text="Stopped"
                        )

        except queue.Empty:

            pass

        self.root.after(
            100,
            self.update_ui
        )


# ============================================================
# Start GUI
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = LiveGenerator(
        root
    )

    root.mainloop()