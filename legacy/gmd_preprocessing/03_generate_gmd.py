from pathlib import Path
import json
import random
import re

import torch
import torch.nn as nn
import mido


# ============================================================
# Settings
# ============================================================

MODEL_FILE = Path("gmd_pretrained.pt")
OUTPUT_DIR = Path("generated_gmd")

NUM_GENERATIONS = 5

# 生成長度：16 小節
TARGET_BARS = 16

# 不同生成風格
TEMPERATURES = [
    0.7,
    0.85,
    1.0,
    1.1,
    1.2,
]

TOP_K = 12

SEED = 42


# ============================================================
# Random seed
# ============================================================

random.seed(SEED)
torch.manual_seed(SEED)

if torch.cuda.is_available():
    torch.cuda.manual_seed_all(SEED)


# ============================================================
# Device
# ============================================================

DEVICE = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)

print()
print("================================")
print("GMD Generation")
print("================================")
print("Device:", DEVICE)

if torch.cuda.is_available():
    print(
        "GPU:",
        torch.cuda.get_device_name(0)
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
    int(index): token
    for token, index in vocab.items()
}

VOCAB_SIZE = checkpoint["vocab_size"]

EMB_DIM = checkpoint["emb_dim"]
HIDDEN_DIM = checkpoint["hidden_dim"]
NUM_LAYERS = checkpoint["num_layers"]

PAD_ID = stoi["<PAD>"]
EOS_ID = stoi["<EOS>"]


print()
print("Vocabulary:", VOCAB_SIZE)
print("Best epoch:", checkpoint.get("epoch"))
print(
    "Best validation loss:",
    checkpoint.get("best_val_loss")
)


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
            input_size=emb_dim,
            hidden_size=hidden_dim,
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

def token_id(token):

    return stoi.get(
        token,
        stoi["<UNK>"]
    )


def decode_time_shift(token):

    match = re.match(
        r"<TIME_SHIFT_(\d+)>",
        token
    )

    if match:

        return int(
            match.group(1)
        )

    return None


def decode_velocity(token):

    match = re.match(
        r"<VEL_(\d+)>",
        token
    )

    if match:

        level = int(
            match.group(1)
        )

        # Training velocity is 1~8.
        # Convert back to MIDI 1~127.
        return max(
            1,
            min(
                127,
                round(
                    level / 8 * 127
                )
            )
        )

    return None


# ============================================================
# Stroke → MIDI note
# ============================================================

STROKE_TO_MIDI = {

    "kick": 36,

    "side_stick": 37,

    "snare": 38,

    "tom_low": 43,
    "tom_mid": 45,
    "tom_high": 48,

    "hihat_closed": 42,
    "hihat_pedal": 44,
    "hihat_open": 46,

    "crash": 49,
    "ride": 51,
    "ride_bell": 53,

    "splash": 55,

    "cowbell": 56,
    "tambourine": 54,

    "clap": 39,
}


def stroke_to_midi(token):

    if not token.startswith(
        "<STROKE_"
    ):
        return None

    stroke = token[
        len("<STROKE_"):-1
    ]

    return STROKE_TO_MIDI.get(
        stroke
    )


# ============================================================
# Grammar constraints
# ============================================================

def allowed_tokens(
    state
):

    tokens = []

    for token, idx in stoi.items():

        # Never generate padding / unknown
        if token in (
            "<PAD>",
            "<UNK>",
            "<BOS>",
            "<SOURCE_GMD>",
            "<BPM_138>",
            "<TS_4-4>",
        ):
            continue

        if state == "WAIT_TIME":

            if token.startswith(
                "<TIME_SHIFT_"
            ):
                tokens.append(idx)

            elif token == "<EOS>":
                tokens.append(idx)

        elif state == "WAIT_HIT_SET":

            if token == "<HIT_SET>":
                tokens.append(idx)

            elif token == "<EOS>":
                tokens.append(idx)

        elif state == "IN_HIT_SET":

            if token.startswith(
                "<STROKE_"
            ):
                tokens.append(idx)

            elif token == "<END_HIT_SET>":
                tokens.append(idx)

        elif state == "WAIT_VELOCITY":

            if token.startswith(
                "<VEL_"
            ):
                tokens.append(idx)

    return tokens


# ============================================================
# Sampling
# ============================================================

def sample_next(
    logits,
    temperature,
    candidates
):

    logits = logits.clone()

    mask = torch.full_like(
        logits,
        float("-inf")
    )

    mask[
        candidates
    ] = logits[
        candidates
    ]

    logits = mask

    logits = logits / temperature

    # Top-K
    if TOP_K is not None:

        values, indices = torch.topk(
            logits,
            min(
                TOP_K,
                len(candidates)
            )
        )

        filtered = torch.full_like(
            logits,
            float("-inf")
        )

        filtered[
            indices
        ] = values

        logits = filtered

    probabilities = torch.softmax(
        logits,
        dim=-1
    )

    next_id = torch.multinomial(
        probabilities,
        num_samples=1
    )

    return next_id.item()


# ============================================================
# Generate token sequence
# ============================================================

def generate_sequence(
    temperature
):

    # Prompt
    tokens = [
        "<BOS>",
        "<SOURCE_GMD>",
        "<TS_4-4>",
        "<BPM_138>",
    ]

    ids = [
        token_id(token)
        for token in tokens
    ]

    input_tensor = torch.tensor(
        [ids],
        dtype=torch.long,
        device=DEVICE
    )

    hidden = None

    # Feed prompt
    with torch.no_grad():

        logits, hidden = model(
            input_tensor,
            hidden
        )

    state = "WAIT_TIME"

    total_ticks = 0

    target_ticks = (
        TARGET_BARS
        * 4
        * 960
    )

    # 4/4, PPQ=960
    max_generated_tokens = 5000

    for step in range(
        max_generated_tokens
    ):

        candidates = allowed_tokens(
            state
        )

        if not candidates:
            break

        current_logits = logits[
            0,
            -1
        ]

        next_id = sample_next(
            current_logits,
            temperature,
            candidates
        )

        next_token = itos[
            next_id
        ]

        tokens.append(
            next_token
        )

        # ----------------------------------------
        # State transition
        # ----------------------------------------

        if state == "WAIT_TIME":

            if next_token == "<EOS>":
                break

            shift = decode_time_shift(
                next_token
            )

            if shift is not None:

                total_ticks += shift

                state = "WAIT_HIT_SET"

        elif state == "WAIT_HIT_SET":

            if next_token == "<EOS>":
                break

            if next_token == "<HIT_SET>":

                state = "IN_HIT_SET"

        elif state == "IN_HIT_SET":

            if next_token == "<END_HIT_SET>":

                state = "WAIT_TIME"

            elif next_token.startswith(
                "<STROKE_"
            ):

                state = "WAIT_VELOCITY"

        elif state == "WAIT_VELOCITY":

            if next_token.startswith(
                "<VEL_"
            ):

                state = "IN_HIT_SET"

        # ----------------------------------------
        # Stop after target duration
        # ----------------------------------------

        if (
            total_ticks
            >= target_ticks
        ):

            # Finish current structure
            break

        # ----------------------------------------
        # Feed token back into model
        # ----------------------------------------

        next_input = torch.tensor(
            [[next_id]],
            dtype=torch.long,
            device=DEVICE
        )

        with torch.no_grad():

            logits, hidden = model(
                next_input,
                hidden
            )

    return tokens


# ============================================================
# Tokens → MIDI
# ============================================================

def tokens_to_midi(
    tokens,
    output_file
):

    mid = mido.MidiFile(
        ticks_per_beat=960
    )

    track = mido.MidiTrack()

    mid.tracks.append(
        track
    )

    # BPM 138
    tempo = mido.bpm2tempo(
        138
    )

    track.append(
        mido.MetaMessage(
            "set_tempo",
            tempo=tempo,
            time=0
        )
    )

    current_time = 0

    pending_notes = []

    i = 0

    while i < len(tokens):

        token = tokens[i]

        # ----------------------------
        # Time shift
        # ----------------------------

        shift = decode_time_shift(
            token
        )

        if shift is not None:

            current_time += shift

            i += 1
            continue

        # ----------------------------
        # HIT_SET
        # ----------------------------

        if token == "<HIT_SET>":

            hit_notes = []

            j = i + 1

            while (
                j < len(tokens)
                and tokens[j]
                != "<END_HIT_SET>"
            ):

                stroke_token = tokens[j]

                midi_note = (
                    stroke_to_midi(
                        stroke_token
                    )
                )

                if midi_note is not None:

                    velocity = 80

                    if (
                        j + 1
                        < len(tokens)
                    ):

                        velocity_value = (
                            decode_velocity(
                                tokens[j + 1]
                            )
                        )

                        if (
                            velocity_value
                            is not None
                        ):
                            velocity = (
                                velocity_value
                            )

                    hit_notes.append(
                        (
                            midi_note,
                            velocity
                        )
                    )

                    j += 2

                else:

                    j += 1

            # Add simultaneous notes
            for note, velocity in hit_notes:

                track.append(
                    mido.Message(
                        "note_on",
                        channel=9,
                        note=note,
                        velocity=velocity,
                        time=0
                    )
                )

                track.append(
                    mido.Message(
                        "note_off",
                        channel=9,
                        note=note,
                        velocity=0,
                        time=120
                    )
                )

            i = j + 1
            continue

        i += 1

    # Ensure a valid ending
    track.append(
        mido.MetaMessage(
            "end_of_track",
            time=0
        )
    )

    mid.save(
        output_file
    )


# ============================================================
# Main
# ============================================================

OUTPUT_DIR.mkdir(
    exist_ok=True
)

print()
print("================================")
print("開始生成")
print("================================")

for index, temperature in enumerate(
    TEMPERATURES,
    start=1
):

    print()
    print(
        f"[{index}/{NUM_GENERATIONS}] "
        f"Temperature = {temperature}"
    )

    tokens = generate_sequence(
        temperature
    )

    output_file = (
        OUTPUT_DIR
        / f"gmd_generation_{index:02d}_T{temperature:.2f}.mid"
    )

    tokens_to_midi(
        tokens,
        output_file
    )

    print(
        "Token 數：",
        len(tokens)
    )

    print(
        "輸出：",
        output_file
    )


print()
print("================================")
print("生成完成")
print("================================")
print(
    "資料夾：",
    OUTPUT_DIR
)