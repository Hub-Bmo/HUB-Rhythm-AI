from pathlib import Path
import json
import random

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader


# ============================================================
# Settings
# ============================================================

SEQUENCE_FILE = Path("gmd_sequences_v2.jsonl")
VOCAB_FILE = Path("vocab.json")

MODEL_FILE = Path("gmd_pretrained.pt")

SEED = 42

EPOCHS = 30

BATCH_SIZE = 8

MAX_SEQ_LEN = 512

EMBED_DIM = 256
HIDDEN_DIM = 512
NUM_LAYERS = 2

LEARNING_RATE = 0.001

GRAD_CLIP = 1.0

NUM_WORKERS = 0


# ============================================================
# Reproducibility
# ============================================================

random.seed(SEED)
torch.manual_seed(SEED)

if torch.cuda.is_available():
    torch.cuda.manual_seed_all(SEED)


# ============================================================
# Device
# ============================================================

if torch.cuda.is_available():

    DEVICE = torch.device("cuda")

    print()
    print("================================")
    print("使用 GPU")
    print(torch.cuda.get_device_name(0))
    print("================================")

else:

    DEVICE = torch.device("cpu")

    print()
    print("================================")
    print("使用 CPU")
    print("================================")


# ============================================================
# Load vocabulary
# ============================================================

with open(
    VOCAB_FILE,
    "r",
    encoding="utf-8"
) as f:

    vocab = json.load(f)


stoi = vocab

itos = {
    int(index): token
    for token, index in vocab.items()
}

VOCAB_SIZE = len(vocab)

PAD_ID = stoi["<PAD>"]
UNK_ID = stoi["<UNK>"]


print()
print("Vocabulary size:", VOCAB_SIZE)


# ============================================================
# Load sequences
# ============================================================

def load_sequences():

    train_sequences = []
    val_sequences = []
    test_sequences = []

    with open(
        SEQUENCE_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            if not line.strip():
                continue

            item = json.loads(line)

            tokens = item["tokens"]

            ids = [
                stoi.get(token, UNK_ID)
                for token in tokens
            ]

            split = str(
                item.get("split", "")
            ).lower()

            if split == "train":

                train_sequences.append(ids)

            elif split in (
                "validation",
                "valid",
                "val"
            ):

                val_sequences.append(ids)

            elif split == "test":

                test_sequences.append(ids)

    return (
        train_sequences,
        val_sequences,
        test_sequences
    )


train_sequences, val_sequences, test_sequences = (
    load_sequences()
)


print()
print("================================")
print("Dataset")
print("================================")
print("Train:", len(train_sequences))
print("Validation:", len(val_sequences))
print("Test:", len(test_sequences))


# ============================================================
# Dataset
# ============================================================

class TokenDataset(Dataset):

    def __init__(
        self,
        sequences,
        max_seq_len=512
    ):

        self.samples = []

        for seq in sequences:

            # 太短的資料無法形成 input / target
            if len(seq) < 2:
                continue

            # 如果超過 MAX_SEQ_LEN，
            # 切成多個 training windows
            start = 0

            while start < len(seq) - 1:

                chunk = seq[
                    start:
                    start + max_seq_len
                ]

                if len(chunk) >= 2:

                    self.samples.append(
                        torch.tensor(
                            chunk,
                            dtype=torch.long
                        )
                    )

                start += max_seq_len

    def __len__(self):

        return len(self.samples)

    def __getitem__(self, index):

        seq = self.samples[index]

        x = seq[:-1]

        y = seq[1:]

        return x, y


train_dataset = TokenDataset(
    train_sequences,
    MAX_SEQ_LEN
)

val_dataset = TokenDataset(
    val_sequences,
    MAX_SEQ_LEN
)

test_dataset = TokenDataset(
    test_sequences,
    MAX_SEQ_LEN
)


print()
print("Training windows:", len(train_dataset))
print("Validation windows:", len(val_dataset))
print("Test windows:", len(test_dataset))


# ============================================================
# Collate
# ============================================================

def collate_fn(batch):

    xs = [item[0] for item in batch]
    ys = [item[1] for item in batch]

    max_len = max(
        x.size(0)
        for x in xs
    )

    batch_x = torch.full(
        (
            len(xs),
            max_len
        ),
        PAD_ID,
        dtype=torch.long
    )

    batch_y = torch.full(
        (
            len(ys),
            max_len
        ),
        PAD_ID,
        dtype=torch.long
    )

    for i, (x, y) in enumerate(
        zip(xs, ys)
    ):

        batch_x[
            i,
            :x.size(0)
        ] = x

        batch_y[
            i,
            :y.size(0)
        ] = y

    return batch_x, batch_y


train_loader = DataLoader(
    train_dataset,
    batch_size=BATCH_SIZE,
    shuffle=True,
    collate_fn=collate_fn,
    num_workers=NUM_WORKERS
)

val_loader = DataLoader(
    val_dataset,
    batch_size=BATCH_SIZE,
    shuffle=False,
    collate_fn=collate_fn,
    num_workers=NUM_WORKERS
)


# ============================================================
# Model
# ============================================================

class LSTMTokenLM(nn.Module):

    def __init__(
        self,
        vocab_size,
        emb_dim=256,
        hidden_dim=512,
        num_layers=2
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

    def forward(self, x):

        x = self.embedding(x)

        output, _ = self.lstm(x)

        logits = self.fc(output)

        return logits


model = LSTMTokenLM(
    VOCAB_SIZE,
    EMBED_DIM,
    HIDDEN_DIM,
    NUM_LAYERS
).to(DEVICE)


print()
print("================================")
print("Model")
print("================================")
print(model)


# ============================================================
# Loss / Optimizer
# ============================================================

criterion = nn.CrossEntropyLoss(
    ignore_index=PAD_ID
)

optimizer = torch.optim.AdamW(
    model.parameters(),
    lr=LEARNING_RATE
)


# ============================================================
# Training
# ============================================================

def run_epoch(
    loader,
    training=True
):

    if training:

        model.train()

    else:

        model.eval()

    total_loss = 0.0
    total_tokens = 0

    for x, y in loader:

        x = x.to(DEVICE)
        y = y.to(DEVICE)

        if training:

            optimizer.zero_grad()

        with torch.set_grad_enabled(training):

            logits = model(x)

            loss = criterion(
                logits.reshape(
                    -1,
                    VOCAB_SIZE
                ),
                y.reshape(-1)
            )

            if training:

                loss.backward()

                torch.nn.utils.clip_grad_norm_(
                    model.parameters(),
                    GRAD_CLIP
                )

                optimizer.step()

        valid_tokens = (
            y != PAD_ID
        ).sum().item()

        total_loss += (
            loss.item()
            * valid_tokens
        )

        total_tokens += valid_tokens

    if total_tokens == 0:

        return 0.0

    return total_loss / total_tokens


# ============================================================
# Main training loop
# ============================================================

best_val_loss = float("inf")

print()
print("================================")
print("開始正式訓練")
print("================================")
print()


for epoch in range(
    1,
    EPOCHS + 1
):

    train_loss = run_epoch(
        train_loader,
        training=True
    )

    val_loss = run_epoch(
        val_loader,
        training=False
    )

    print(
        f"Epoch {epoch:02d}/{EPOCHS}"
        f" | Train Loss: {train_loss:.4f}"
        f" | Val Loss: {val_loss:.4f}"
    )

    # ----------------------------
    # Save best model
    # ----------------------------

    if val_loss < best_val_loss:

        best_val_loss = val_loss

        torch.save(
            {
                "model_state_dict":
                    model.state_dict(),

                "vocab_size":
                    VOCAB_SIZE,

                "emb_dim":
                    EMBED_DIM,

                "hidden_dim":
                    HIDDEN_DIM,

                "num_layers":
                    NUM_LAYERS,

                "best_val_loss":
                    best_val_loss,

                "epoch":
                    epoch,

                "vocab":
                    vocab,
            },
            MODEL_FILE
        )

        print(
            "  ★ 儲存最佳模型 →",
            MODEL_FILE
        )


print()
print("================================")
print("訓練完成")
print("================================")
print("Best validation loss:",
      best_val_loss)
print("Model:",
      MODEL_FILE)