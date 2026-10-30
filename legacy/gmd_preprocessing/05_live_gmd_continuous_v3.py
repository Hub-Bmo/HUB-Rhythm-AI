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

BARS_PER_CHUNK = 8

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
    """
    Render exactly one 8-bar audio buffer.

    v2 added +1 second of padding and then called sd.play() for the
    whole array. That padding became an audible gap between chunks.
    v3 renders the core musical duration only and feeds it into one
    persistent OutputStream.
    """
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
        duration_seconds * SAMPLE_RATE
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
# Visualization helpers
# ============================================================

VIS_ROWS = [
    ("Kick", {"kick"}),
    ("Snare", {"snare"}),
    ("Hi-hat", {"hihat_closed", "hihat_pedal", "hihat_open"}),
    ("Ride", {"ride", "ride_bell"}),
    ("Tom", {"tom_low", "tom_mid", "tom_high"}),
    ("Cymbal", {"crash", "splash"}),
]


def event_column(tick):
    """
    8 bars x 16th-note grid = 128 columns.
    PPQ=960, so one 16th = 240 ticks.
    """
    return max(
        0,
        min(
            BARS_PER_CHUNK * 16 - 1,
            int(round(tick / 240))
        )
    )


# ============================================================
# GUI + seamless audio engine
# ============================================================

class App:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "GMD Live Groove Generator v3"
        )

        self.root.geometry(
            "1180x760"
        )

        self.stop_event = threading.Event()

        self.running = False

        # Each item:
        # {
        #   "audio": np.ndarray,
        #   "events": [...],
        #   "bar_start": int
        # }
        self.audio_queue = queue.Queue(
            maxsize=BUFFER_SIZE
        )

        self.status_queue = queue.Queue()

        self.generator = None

        self.bar_count = 0

        self.stream = None
        self.current_audio = None
        self.current_events = []
        self.current_event_index = 0
        self.current_sample_pos = 0
        self.audio_lock = threading.Lock()

        self.visual_cells = {}
        self.current_visual_chunk = None
        self.visual_hits = []

        self.build_ui()

        self.root.after(
            50,
            self.update_ui
        )

    # ========================================================
    # UI
    # ========================================================

    def build_ui(self):

        title = tk.Label(
            self.root,
            text="GMD Live Groove Generator v3",
            font=("Arial", 22, "bold")
        )
        title.pack(pady=(16, 4))

        tk.Label(
            self.root,
            text="8 bars / chunk · persistent OutputStream · real generated-event visualization",
            font=("Arial", 11)
        ).pack(pady=(0, 12))

        controls = tk.Frame(self.root)
        controls.pack(pady=5)

        tk.Label(
            controls,
            text="BPM"
        ).grid(
            row=0,
            column=0,
            padx=8
        )

        self.bpm_var = tk.IntVar(
            value=DEFAULT_BPM
        )

        tk.Spinbox(
            controls,
            from_=60,
            to=200,
            textvariable=self.bpm_var,
            width=7
        ).grid(
            row=0,
            column=1,
            padx=(0, 18)
        )

        tk.Label(
            controls,
            text="Temperature"
        ).grid(
            row=0,
            column=2,
            padx=8
        )

        self.temp_var = tk.DoubleVar(
            value=DEFAULT_TEMPERATURE
        )

        tk.Scale(
            controls,
            from_=0.5,
            to=1.3,
            resolution=0.05,
            orient="horizontal",
            variable=self.temp_var,
            length=220
        ).grid(
            row=0,
            column=3,
            padx=(0, 18)
        )

        self.start_button = tk.Button(
            controls,
            text="▶ START",
            font=("Arial", 13, "bold"),
            width=10,
            command=self.start
        )
        self.start_button.grid(
            row=0,
            column=4,
            padx=5
        )

        self.stop_button = tk.Button(
            controls,
            text="■ STOP",
            font=("Arial", 13, "bold"),
            width=10,
            command=self.stop,
            state="disabled"
        )
        self.stop_button.grid(
            row=0,
            column=5,
            padx=5
        )

        info = tk.Frame(self.root)
        info.pack(fill="x", padx=24, pady=10)

        self.status_label = tk.Label(
            info,
            text="Ready",
            font=("Arial", 13)
        )
        self.status_label.pack(side="left")

        self.bar_label = tk.Label(
            info,
            text="Generated bars: 0",
            font=("Arial", 13)
        )
        self.bar_label.pack(side="left", padx=30)

        self.buffer_label = tk.Label(
            info,
            text="Audio buffer: 0 / 3",
            font=("Arial", 13)
        )
        self.buffer_label.pack(side="left")

        # -------------------------
        # 8-bar visualization
        # -------------------------

        outer = tk.Frame(
            self.root,
            bd=1,
            relief="solid"
        )
        outer.pack(
            fill="both",
            expand=True,
            padx=24,
            pady=(4, 18)
        )

        self.canvas = tk.Canvas(
            outer,
            highlightthickness=0
        )

        hscroll = tk.Scrollbar(
            outer,
            orient="horizontal",
            command=self.canvas.xview
        )

        vscroll = tk.Scrollbar(
            outer,
            orient="vertical",
            command=self.canvas.yview
        )

        self.canvas.configure(
            xscrollcommand=hscroll.set,
            yscrollcommand=vscroll.set
        )

        self.canvas.grid(
            row=0,
            column=0,
            sticky="nsew"
        )

        vscroll.grid(
            row=0,
            column=1,
            sticky="ns"
        )

        hscroll.grid(
            row=1,
            column=0,
            sticky="ew"
        )

        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        self.draw_grid()

        tk.Label(
            self.root,
            text=(
                "亮色 = 模型實際生成的 hit　"
                "│　藍框 = 目前播放位置　"
                "│　每格 = 1/16"
            ),
            font=("Arial", 10)
        ).pack(
            pady=(0, 12)
        )

    def draw_grid(self):

        self.canvas.delete("all")

        left = 90
        top = 42
        cell_w = 30
        cell_h = 42

        total_cols = BARS_PER_CHUNK * 16

        # Header: bar numbers
        for bar in range(BARS_PER_CHUNK):

            x0 = (
                left
                + bar * 16 * cell_w
            )

            self.canvas.create_text(
                x0 + 8 * cell_w,
                18,
                text=f"Bar {bar + 1}",
                font=("Arial", 10, "bold")
            )

        # Beat labels
        for col in range(total_cols):

            x = (
                left
                + col * cell_w
                + cell_w / 2
            )

            within_bar = col % 16

            if within_bar % 4 == 0:
                label = str(within_bar // 4 + 1)
            else:
                label = ""

            self.canvas.create_text(
                x,
                36,
                text=label,
                font=("Arial", 8)
            )

        row_y = {}

        for row_index, (
            name,
            strokes
        ) in enumerate(VIS_ROWS):

            y = (
                top
                + row_index * cell_h
            )

            row_y[name] = y

            self.canvas.create_text(
                45,
                y + cell_h / 2,
                text=name,
                font=("Arial", 10)
            )

            for col in range(total_cols):

                x = (
                    left
                    + col * cell_w
                )

                if col % 16 == 0:
                    outline = "#777777"
                    width = 2
                elif col % 4 == 0:
                    outline = "#BBBBBB"
                    width = 1
                else:
                    outline = "#DDDDDD"
                    width = 1

                rect = self.canvas.create_rectangle(
                    x + 1,
                    y + 2,
                    x + cell_w - 1,
                    y + cell_h - 2,
                    fill="white",
                    outline=outline,
                    width=width
                )

                self.visual_cells[
                    (name, col)
                ] = rect

        self.canvas.configure(
            scrollregion=(
                0,
                0,
                left + total_cols * cell_w + 20,
                top + len(VIS_ROWS) * cell_h + 20
            )
        )

    # ========================================================
    # Visualization
    # ========================================================

    def clear_visual_hits(self):

        for item in self.visual_hits:

            try:
                self.canvas.itemconfigure(
                    item,
                    fill="white"
                )
            except Exception:
                pass

        self.visual_hits.clear()

    def show_chunk(self, events):

        self.clear_visual_hits()

        for tick, stroke, velocity in events:

            col = event_column(tick)

            row_name = None

            for name, strokes in VIS_ROWS:

                if stroke in strokes:
                    row_name = name
                    break

            if row_name is None:
                continue

            rect = self.visual_cells.get(
                (row_name, col)
            )

            if rect is None:
                continue

            self.canvas.itemconfigure(
                rect,
                fill="#d9dcff"
            )

            self.visual_hits.append(rect)

    def update_playhead(self, sample_pos):

        # Convert current sample position into
        # 1/16 grid position.
        bpm = max(
            1,
            int(self.bpm_var.get())
        )

        ticks_per_second = (
            PPQ * bpm / 60
        )

        tick = (
            sample_pos
            / SAMPLE_RATE
            * ticks_per_second
        )

        col = event_column(tick)

        # Remove old playhead tags.
        self.canvas.delete("playhead")

        left = 90
        cell_w = 30

        x = (
            left
            + col * cell_w
            + cell_w / 2
        )

        self.canvas.create_line(
            x,
            40,
            x,
            40 + len(VIS_ROWS) * 42,
            fill="#3030cc",
            width=2,
            tags="playhead"
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

        self.current_audio = None
        self.current_events = []
        self.current_event_index = 0
        self.current_sample_pos = 0

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
            text="Preparing 8-bar buffer..."
        )

        producer = threading.Thread(
            target=self.producer_loop,
            daemon=True
        )

        producer.start()

        consumer = threading.Thread(
            target=self.consumer_loop,
            daemon=True
        )

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

                item = {
                    "audio": audio,
                    "events": events,
                    "bar_start": (
                        self.generator.total_bars
                        - BARS_PER_CHUNK
                        + 1
                    )
                }

                self.audio_queue.put(
                    item
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
    # Seamless OutputStream
    # ========================================================

    def audio_callback(
        self,
        outdata,
        frames,
        time_info,
        status
    ):

        if status:

            self.status_queue.put(
                (
                    "stream_status",
                    str(status)
                )
            )

        outdata.fill(0)

        written = 0

        while written < frames:

            if self.current_audio is None:

                try:

                    item = (
                        self.audio_queue.get_nowait()
                    )

                    self.current_audio = (
                        item["audio"]
                    )

                    self.current_events = (
                        item["events"]
                    )

                    self.current_event_index = 0
                    self.current_sample_pos = 0

                    self.status_queue.put(
                        (
                            "chunk_start",
                            (
                                item["bar_start"],
                                item["events"]
                            )
                        )
                    )

                except queue.Empty:

                    # No chunk ready yet.
                    # Output silence instead of blocking
                    # the PortAudio callback.
                    break

            remaining = (
                len(self.current_audio)
                - self.current_sample_pos
            )

            count = min(
                frames - written,
                remaining
            )

            if count <= 0:

                self.current_audio = None
                continue

            outdata[
                written:written + count,
                0
            ] = self.current_audio[
                self.current_sample_pos:
                self.current_sample_pos + count
            ]

            # Trigger visualization messages for events
            # whose sample position has now been reached.
            bpm = max(
                1,
                int(self.bpm_var.get())
            )

            ticks_per_second = (
                PPQ * bpm / 60
            )

            end_sample = (
                self.current_sample_pos
                + count
            )

            while (
                self.current_event_index
                < len(self.current_events)
            ):

                tick, stroke, velocity = (
                    self.current_events[
                        self.current_event_index
                    ]
                )

                event_sample = int(
                    (
                        tick
                        / ticks_per_second
                    )
                    * SAMPLE_RATE
                )

                if event_sample > end_sample:
                    break

                self.status_queue.put(
                    (
                        "hit",
                        (
                            tick,
                            stroke,
                            velocity
                        )
                    )
                )

                self.current_event_index += 1

            self.current_sample_pos += count
            written += count

            if (
                self.current_sample_pos
                >= len(self.current_audio)
            ):

                self.current_audio = None
                self.current_events = []
                self.current_event_index = 0
                self.current_sample_pos = 0

        # Update playhead based on current chunk.
        self.status_queue.put(
            (
                "playhead",
                self.current_sample_pos
            )
        )

    def consumer_loop(self):

        try:

            # Wait until we have at least one full chunk.
            while (
                not self.stop_event.is_set()
                and self.audio_queue.empty()
            ):

                self.stop_event.wait(
                    0.02
                )

            if self.stop_event.is_set():
                return

            self.status_queue.put(
                (
                    "playing",
                    None
                )
            )

            self.stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self.audio_callback,
                blocksize=1024,
                latency="low"
            )

            self.stream.start()

            while not self.stop_event.is_set():

                self.stop_event.wait(
                    0.05
                )

            self.stream.stop()
            self.stream.close()
            self.stream = None

        except Exception as e:

            self.status_queue.put(
                (
                    "error",
                    str(e)
                )
            )

        finally:

            try:

                if self.stream is not None:
                    self.stream.stop()
                    self.stream.close()

            except Exception:
                pass

            self.stream = None

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

        try:

            if self.stream is not None:
                self.stream.stop()
                self.stream.close()

        except Exception:
            pass

        self.stream = None

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
                        text="Playing continuously..."
                    )

                elif message == "chunk_start":

                    bar_start, events = value

                    self.show_chunk(
                        events
                    )

                    self.status_label.config(
                        text=(
                            f"Playing → "
                            f"Bar {bar_start}"
                            f"-{bar_start + BARS_PER_CHUNK - 1}"
                        )
                    )

                elif message == "hit":

                    tick, stroke, velocity = value

                    # We do not redraw the whole grid here.
                    # The chunk is already displayed when generated.

                elif message == "playhead":

                    if self.current_audio is not None:
                        self.update_playhead(
                            value
                        )

                elif message == "stream_status":

                    print(
                        "Audio:",
                        value
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
            50,
            self.update_ui
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = App(root)

    root.mainloop()
