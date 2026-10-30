from pathlib import Path
import threading
import queue

import numpy as np
import sounddevice as sd
import tkinter as tk
import torch
import torch.nn as nn


# ============================================================
# Settings
# ============================================================

MODEL_FILE = Path("gmd_pretrained.pt")

PPQ = 960
SAMPLE_RATE = 44100

DEFAULT_BPM = 138
DEFAULT_TEMPERATURE = 0.85

BARS_PER_CHUNK = 4

# 預先準備幾個 chunk
BUFFER_SIZE = 3

TOP_K = 12

DEVICE = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)


# ============================================================
# Load checkpoint
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


print("================================")
print("GMD Continuous Generator v2")
print("================================")
print("Device:", DEVICE)

if torch.cuda.is_available():
    print(
        "GPU:",
        torch.cuda.get_device_name(0)
    )


# ============================================================
# Token helpers
# ============================================================

def token_id(token):
    return stoi.get(
        token,
        stoi["<UNK>"]
    )


def time_shift_value(token):

    if token.startswith("<TIME_SHIFT_"):

        return int(
            token[len("<TIME_SHIFT_"):-1]
        )

    return None


def velocity_value(token):

    if token.startswith("<VEL_"):

        level = int(
            token[len("<VEL_"):-1]
        )

        return max(
            1,
            min(
                127,
                round(level / 8 * 127)
            )
        )

    return None


def stroke_name(token):

    if token.startswith("<STROKE_"):

        return token[len("<STROKE_"):-1]

    return None


# ============================================================
# Drum synthesis
# ============================================================

def make_kick(velocity, duration=0.25):

    n = int(SAMPLE_RATE * duration)

    t = np.arange(n) / SAMPLE_RATE

    frequency = (
        120 * np.exp(-t * 18)
        + 45
    )

    phase = (
        2 * np.pi
        * np.cumsum(frequency)
        / SAMPLE_RATE
    )

    envelope = np.exp(-t * 14)

    sound = (
        np.sin(phase)
        * envelope
    )

    return sound * (velocity / 127)


def make_snare(velocity, duration=0.18):

    n = int(SAMPLE_RATE * duration)

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(-t * 22)

    tone = (
        np.sin(
            2 * np.pi * 180 * t
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


def make_hihat(velocity, duration=0.07):

    n = int(SAMPLE_RATE * duration)

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(-t * 65)

    return (
        noise
        * envelope
        * (velocity / 127)
    )


def make_tom(
    velocity,
    frequency,
    duration=0.22
):

    n = int(SAMPLE_RATE * duration)

    t = np.arange(n) / SAMPLE_RATE

    envelope = np.exp(-t * 15)

    sound = (
        np.sin(
            2 * np.pi * frequency * t
        )
        * envelope
    )

    return sound * (velocity / 127)


def make_cymbal(
    velocity,
    duration=0.35
):

    n = int(SAMPLE_RATE * duration)

    t = np.arange(n) / SAMPLE_RATE

    noise = np.random.randn(n)

    envelope = np.exp(-t * 7)

    return (
        noise
        * envelope
        * (velocity / 127)
        * 0.5
    )


def drum_sound(stroke, velocity):

    if stroke == "kick":
        return make_kick(velocity)

    if stroke == "snare":
        return make_snare(velocity)

    if stroke in (
        "hihat_closed",
        "hihat_pedal"
    ):
        return make_hihat(velocity)

    if stroke == "hihat_open":
        return make_hihat(
            velocity,
            duration=0.25
        )

    if stroke == "tom_low":
        return make_tom(
            velocity,
            100
        )

    if stroke == "tom_mid":
        return make_tom(
            velocity,
            140
        )

    if stroke == "tom_high":
        return make_tom(
            velocity,
            190
        )

    if stroke in (
        "crash",
        "ride",
        "ride_bell",
        "splash"
    ):
        return make_cymbal(velocity)

    return None


# ============================================================
# Grammar
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

    filtered /= temperature

    k = min(
        TOP_K,
        len(candidates)
    )

    values, indices = torch.topk(
        filtered,
        k
    )

    top_logits = torch.full_like(
        filtered,
        float("-inf")
    )

    top_logits[
        indices
    ] = values

    probabilities = torch.softmax(
        top_logits,
        dim=-1
    )

    return torch.multinomial(
        probabilities,
        1
    ).item()


# ============================================================
# Continuous Generator
# ============================================================

class ContinuousGenerator:

    def __init__(
        self,
        temperature
    ):

        self.temperature = temperature

        self.hidden = None

        self.logits = None

        self.started = False

        self.total_bars = 0

        self.state = "TIME"

        self.current_stroke = None

        self.current_velocity = 80

        self.elapsed_ticks = 0

        self.target_ticks = (
            BARS_PER_CHUNK
            * 4
            * PPQ
        )


    def start(self):

        prompt = [
            "<BOS>",
            "<SOURCE_GMD>",
            "<TS_4-4>",
            "<BPM_138>",
        ]

        ids = [
            token_id(x)
            for x in prompt
        ]

        tensor = torch.tensor(
            [ids],
            dtype=torch.long,
            device=DEVICE
        )

        with torch.inference_mode():

            self.logits, self.hidden = (
                model(tensor)
            )

        self.started = True


    def generate_chunk(
        self,
        stop_event
    ):

        if not self.started:

            self.start()

        events = []

        self.elapsed_ticks = 0

        self.target_ticks = (
            BARS_PER_CHUNK
            * 4
            * PPQ
        )

        while (
            self.elapsed_ticks
            < self.target_ticks
        ):

            if stop_event.is_set():
                return None

            candidates = (
                candidates_for_state(
                    self.state
                )
            )

            next_id = sample_token(
                self.logits[0, -1],
                self.temperature,
                candidates
            )

            if next_id is None:
                return None

            token = itos[next_id]


            # ----------------------------
            # TIME
            # ----------------------------

            if self.state == "TIME":

                shift = time_shift_value(
                    token
                )

                if shift is not None:

                    self.elapsed_ticks += shift

                    self.state = "HIT"


            # ----------------------------
            # HIT
            # ----------------------------

            elif self.state == "HIT":

                if token == "<HIT_SET>":

                    self.state = "STROKE"


            # ----------------------------
            # STROKE
            # ----------------------------

            elif self.state == "STROKE":

                if token == "<END_HIT_SET>":

                    self.state = "TIME"

                else:

                    self.current_stroke = (
                        stroke_name(token)
                    )

                    self.state = "VEL"


            # ----------------------------
            # VELOCITY
            # ----------------------------

            elif self.state == "VEL":

                velocity = velocity_value(
                    token
                )

                if velocity is not None:

                    self.current_velocity = (
                        velocity
                    )

                    events.append(
                        (
                            self.elapsed_ticks,
                            self.current_stroke,
                            self.current_velocity
                        )
                    )

                    self.state = "STROKE"


            # ----------------------------
            # IMPORTANT
            # Feed generated token back
            # into SAME LSTM state.
            # ----------------------------

            tensor = torch.tensor(
                [[next_id]],
                dtype=torch.long,
                device=DEVICE
            )

            with torch.inference_mode():

                self.logits, self.hidden = (
                    model(
                        tensor,
                        self.hidden
                    )
                )


        self.total_bars += BARS_PER_CHUNK

        return events


# ============================================================
# Audio rendering
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
                :end - start
            ]


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

class App:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "GMD Live Groove Generator v2"
        )

        self.root.geometry(
            "700x520"
        )

        self.stop_event = (
            threading.Event()
        )

        self.running = False

        self.audio_queue = queue.Queue(
            maxsize=BUFFER_SIZE
        )

        self.status_queue = queue.Queue()

        self.generator = None

        self.bar_count = 0

        self.build_ui()

        self.root.after(
            100,
            self.update_ui
        )


    def build_ui(self):

        tk.Label(
            self.root,
            text="GMD Live Groove Generator",
            font=("Arial", 22, "bold")
        ).pack(
            pady=20
        )


        frame = tk.Frame(
            self.root
        )

        frame.pack(
            pady=10
        )


        # BPM

        tk.Label(
            frame,
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
            frame,
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
            frame,
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

        tk.Scale(
            frame,
            from_=0.5,
            to=1.3,
            resolution=0.05,
            orient="horizontal",
            variable=self.temp_var,
            length=280
        ).grid(
            row=1,
            column=1
        )


        tk.Label(
            self.root,
            text="4 bars / generation buffer",
            font=("Arial", 11)
        ).pack(
            pady=5
        )


        self.status_label = tk.Label(
            self.root,
            text="Ready",
            font=("Arial", 15)
        )

        self.status_label.pack(
            pady=20
        )


        self.bar_label = tk.Label(
            self.root,
            text="Generated bars: 0",
            font=("Arial", 20)
        )

        self.bar_label.pack(
            pady=10
        )


        buttons = tk.Frame(
            self.root
        )

        buttons.pack(
            pady=20
        )


        self.start_button = tk.Button(
            buttons,
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
            buttons,
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


        self.buffer_label = tk.Label(
            self.root,
            text="Audio buffer: 0 / 3",
            font=("Arial", 11)
        )

        self.buffer_label.pack(
            pady=10
        )


        tk.Label(
            self.root,
            text=(
                "GMD LSTM\n"
                "Continuous hidden state + background generation"
            ),
            justify="left"
        ).pack(
            pady=15
        )


    # ========================================================
    # Start
    # ========================================================

    def start(self):

        if self.running:
            return

        self.running = True

        self.stop_event.clear()

        self.audio_queue = queue.Queue(
            maxsize=BUFFER_SIZE
        )

        self.bar_count = 0

        temperature = float(
            self.temp_var.get()
        )

        self.generator = (
            ContinuousGenerator(
                temperature
            )
        )

        self.start_button.config(
            state="disabled"
        )

        self.stop_button.config(
            state="normal"
        )

        self.status_label.config(
            text="Preparing buffer..."
        )


        producer = threading.Thread(
            target=self.producer_loop,
            daemon=True
        )

        consumer = threading.Thread(
            target=self.consumer_loop,
            daemon=True
        )

        producer.start()

        consumer.start()


    # ========================================================
    # Producer
    # ========================================================

    def producer_loop(self):

        try:

            while not self.stop_event.is_set():

                if self.audio_queue.full():

                    self.stop_event.wait(
                        0.01
                    )

                    continue


                events = (
                    self.generator.generate_chunk(
                        self.stop_event
                    )
                )

                if events is None:
                    break


                bpm = int(
                    self.bpm_var.get()
                )

                audio = render_audio(
                    events,
                    bpm
                )

                self.audio_queue.put(
                    audio
                )

                self.status_queue.put(
                    (
                        "generated",
                        self.generator.total_bars
                    )
                )


        except Exception as e:

            self.status_queue.put(
                (
                    "error",
                    str(e)
                )
            )

        finally:

            self.status_queue.put(
                (
                    "producer_done",
                    None
                )
            )


    # ========================================================
    # Consumer
    # ========================================================

    def consumer_loop(self):

        try:

            while not self.stop_event.is_set():

                try:

                    audio = (
                        self.audio_queue.get(
                            timeout=0.1
                        )
                    )

                except queue.Empty:

                    continue


                bpm = int(
                    self.bpm_var.get()
                )

                self.status_queue.put(
                    (
                        "playing",
                        self.bar_count + BARS_PER_CHUNK
                    )
                )

                sd.play(
                    audio,
                    SAMPLE_RATE,
                    blocking=True
                )

                if self.stop_event.is_set():

                    sd.stop()

                    break

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

        finally:

            sd.stop()

            self.status_queue.put(
                (
                    "consumer_done",
                    None
                )
            )


    # ========================================================
    # Stop
    # ========================================================

    def stop(self):

        if not self.running:
            return

        self.stop_event.set()

        sd.stop()

        self.status_label.config(
            text="Stopping..."
        )


    # ========================================================
    # UI updates
    # ========================================================

    def update_ui(self):

        try:

            while True:

                message, value = (
                    self.status_queue.get_nowait()
                )


                if message == "generated":

                    self.bar_label.config(
                        text=(
                            f"Generated bars: "
                            f"{value}"
                        )
                    )


                elif message == "playing":

                    self.status_label.config(
                        text=(
                            f"Playing → "
                            f"Bar {value}"
                        )
                    )


                elif message == "error":

                    self.status_label.config(
                        text=(
                            "ERROR: "
                            + value
                        )
                    )

                    print(
                        "\nERROR:",
                        value
                    )


                elif message == "producer_done":

                    pass


                elif message == "consumer_done":

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


        try:

            self.buffer_label.config(
                text=(
                    f"Audio buffer: "
                    f"{self.audio_queue.qsize()} "
                    f"/ {BUFFER_SIZE}"
                )
            )

        except Exception:
            pass


        self.root.after(
            100,
            self.update_ui
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = App(root)

    root.mainloop()