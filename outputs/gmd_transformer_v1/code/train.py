"""Unattended GPU training with atomic checkpoints and epoch/batch resumption."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import json
import math
import os
from pathlib import Path
import random
import shutil
import sys
import time
import traceback

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

from .data import TokenDataset, collate, load_data
from .model import ModelConfig, RhythmTransformer


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "projects" / "gmd_transformer_v1" / "data"
DEFAULT_RUN = ROOT / "outputs" / "gmd_transformer_v1"


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def atomic_checkpoint(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    torch.save(value, temp)
    temp.replace(path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def learning_rate(step, total_steps, warmup, peak):
    if step < warmup:
        return peak * (step + 1) / max(warmup, 1)
    progress = min(1.0, (step - warmup) / max(1, total_steps - warmup))
    return peak * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * progress)))


def parser():
    p = argparse.ArgumentParser(description="Train GMD Causal Transformer v1")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA / "gmd_sequences_v2.jsonl")
    p.add_argument("--vocab", type=Path, default=DEFAULT_DATA / "vocab.json")
    p.add_argument("--run", type=Path, default=DEFAULT_RUN)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--lr", type=float, default=0.0003)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--patience", type=int, default=6)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--save-every", type=int, default=100)
    p.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    p.add_argument("--resume", type=Path)
    p.add_argument("--init-from", type=Path, help="Fine-tune an existing Transformer with an append-only vocabulary")
    p.add_argument("--max-batches", type=int, default=0, help="Smoke test only; never used for the real run")
    p.add_argument("--no-samples", action="store_true")
    return p


def train(args):
    if args.resume and args.init_from:
        raise ValueError("Choose resume or init-from")
    if args.epochs < 1 or args.batch_size < 1 or args.patience < 1 or args.save_every < 1:
        raise ValueError("epochs, batch-size, patience and save-every must be positive")
    args.run = args.run.resolve()
    args.run.mkdir(parents=True, exist_ok=True)
    if (args.run / "last.pt").exists() and not args.resume:
        raise ValueError("Existing run has a checkpoint; use --resume or a new run directory")
    torch.set_num_threads(4)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable: refusing an accidental overnight CPU training run")
    device = torch.device(args.device)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cuda.matmul.allow_tf32 = True
    vocab, splits, metadata = load_data(args.data, args.vocab)
    datasets = {split: TokenDataset(rows) for split, rows in splits.items()}
    metadata["window_counts"] = {k: len(v) for k, v in datasets.items()}
    checkpoint = None
    if args.resume or args.init_from:
        checkpoint = torch.load(args.resume or args.init_from, map_location="cpu", weights_only=True)
        if checkpoint.get("architecture") != "gmd-causal-transformer-v1":
            raise ValueError("Checkpoint is not a compatible Transformer")
    config = ModelConfig(vocab_size=len(vocab), pad_id=vocab["<PAD>"])
    if checkpoint:
        config = ModelConfig(**checkpoint["model_config"])
        original = RhythmTransformer(config)
        original.load_state_dict(checkpoint["model_state_dict"])
        if args.resume:
            if checkpoint["vocab"] != vocab or checkpoint["data_metadata"]["sequence_sha256"] != metadata["sequence_sha256"]:
                raise ValueError("Resume data/vocabulary differs from the saved run")
            for name in ("batch_size", "seed", "lr", "warmup"):
                if vars(args)[name] != checkpoint["training_args"][name]:
                    raise ValueError(f"Resume must preserve {name}={checkpoint['training_args'][name]}")
            model = original
        else:
            for token, token_id in checkpoint["vocab"].items():
                if vocab.get(token) != token_id:
                    raise ValueError("Fine-tuning vocabulary must keep every original token ID; append new tokens only")
            model = original.expanded(len(vocab))
            config = model.config
    else:
        model = RhythmTransformer(config)
    model = model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=(0.9, 0.95), weight_decay=0.01)
    use_amp = device.type == "cuda"
    amp_dtype = torch.bfloat16 if use_amp and torch.cuda.is_bf16_supported() else torch.float16
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp and amp_dtype == torch.float16)
    state = {"epoch": 1, "next_batch": 0, "epoch_completed": False, "global_step": 0,
             "best_val_loss": None, "best_epoch": None, "bad_epochs": 0, "history": [],
             "running_loss_sum": 0.0, "running_tokens": 0}
    if args.resume:
        state = checkpoint["training_state"]
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scaler.load_state_dict(checkpoint["scaler_state_dict"])
        torch.set_rng_state(checkpoint["rng_state"])
        random.setstate(checkpoint["python_rng_state"])
        if use_amp and checkpoint.get("cuda_rng_states"):
            torch.cuda.set_rng_state_all(checkpoint["cuda_rng_states"])
        if state["epoch_completed"]:
            state["epoch"] += 1
            state["epoch_completed"] = False
            state["next_batch"] = 0
            state["running_loss_sum"] = 0.0
            state["running_tokens"] = 0
    if not (args.run / "vocab.json").exists():
        shutil.copyfile(args.vocab, args.run / "vocab.json")
    source_dir = args.run / "code"
    source_dir.mkdir(exist_ok=True)
    for module in ("train.py", "model.py", "data.py", "generate.py"):
        source = Path(__file__).parent / module
        if source.exists() and not (source_dir / module).exists():
            shutil.copyfile(source, source_dir / module)
    training_args = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    machine = {"python": sys.version, "python_executable": sys.executable, "torch": str(torch.__version__),
               "cuda": torch.version.cuda, "device": str(device), "gpu": torch.cuda.get_device_name(0) if use_amp else None,
               "precision": str(amp_dtype) if use_amp else "float32", "parameters": sum(p.numel() for p in model.parameters())}
    atomic_json(args.run / "config.json", {"training": training_args, "model": asdict(config), "data": metadata, "machine": machine})
    atomic_json(args.run / "split_manifest.json", {k: [{a: b for a, b in row.items() if a != "ids"} for row in v] for k, v in splits.items()})
    print(json.dumps({"event": "training_start", "data": metadata, "model": asdict(config), "machine": machine}, ensure_ascii=False), flush=True)
    start_time = time.monotonic()
    batches_per_epoch = math.ceil(len(datasets["train"]) / args.batch_size)
    if args.max_batches:
        batches_per_epoch = min(batches_per_epoch, args.max_batches)
    total_steps = batches_per_epoch * args.epochs

    def amp_context():
        return torch.autocast(device_type="cuda", dtype=amp_dtype) if use_amp else nullcontext()

    def save_checkpoint(path):
        atomic_checkpoint(path, {"architecture": "gmd-causal-transformer-v1", "model_config": asdict(config),
            "model_state_dict": model.state_dict(), "optimizer_state_dict": optimizer.state_dict(),
            "scaler_state_dict": scaler.state_dict(), "vocab": vocab, "training_state": deepcopy_state(state),
            "training_args": training_args, "data_metadata": metadata, "rng_state": torch.get_rng_state(),
            "cuda_rng_states": torch.cuda.get_rng_state_all() if use_amp else [], "python_rng_state": random.getstate(),
            "epoch": state["epoch"], "best_val_loss": state["best_val_loss"], "saved_at": utc_now()})

    def status(phase, **extras):
        atomic_json(args.run / "status.json", {"phase": phase, "updated_at": utc_now(), "pid": os.getpid(),
            "epoch": state["epoch"], "max_epochs": args.epochs, "batch": state["next_batch"],
            "batches_per_epoch": batches_per_epoch, "global_step": state["global_step"],
            "best_val_loss": state["best_val_loss"], "best_epoch": state["best_epoch"],
            "elapsed_seconds_this_process": round(time.monotonic() - start_time, 1), **extras})

    @torch.inference_mode()
    def evaluate(split):
        model.eval()
        loss_sum, count = 0.0, 0
        loader = DataLoader(datasets[split], batch_size=args.batch_size, shuffle=False,
                            collate_fn=partial(collate, pad_id=config.pad_id), num_workers=0)
        for batch_index, (x, y) in enumerate(loader):
            if args.max_batches and batch_index >= args.max_batches:
                break
            x, y = x.to(device), y.to(device)
            with amp_context():
                logits = model(x)
                loss = F.cross_entropy(logits.flatten(0, 1), y.flatten(), ignore_index=config.pad_id, reduction="sum")
            loss_sum += float(loss)
            count += int((y != config.pad_id).sum())
        return loss_sum / count if count else None

    try:
        for epoch in range(state["epoch"], args.epochs + 1):
            epoch_started = time.monotonic()
            state["epoch"] = epoch
            model.train()
            generator = torch.Generator().manual_seed(args.seed + epoch)
            loader = DataLoader(datasets["train"], batch_size=args.batch_size, shuffle=True, generator=generator,
                                collate_fn=partial(collate, pad_id=config.pad_id), num_workers=0)
            for batch_index, (x, y) in enumerate(loader):
                if batch_index < state["next_batch"]:
                    continue
                if args.max_batches and batch_index >= args.max_batches:
                    break
                x, y = x.to(device), y.to(device)
                optimizer.zero_grad(set_to_none=True)
                lr = learning_rate(state["global_step"], total_steps, min(args.warmup, max(1, total_steps // 10)), args.lr)
                for group in optimizer.param_groups:
                    group["lr"] = lr
                with amp_context():
                    logits = model(x)
                    loss = F.cross_entropy(logits.flatten(0, 1), y.flatten(), ignore_index=config.pad_id)
                if not torch.isfinite(loss):
                    raise RuntimeError("Non-finite training loss")
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
                scaler.step(optimizer)
                scaler.update()
                count = int((y != config.pad_id).sum())
                state["running_loss_sum"] += float(loss.detach()) * count
                state["running_tokens"] += count
                state["global_step"] += 1
                state["next_batch"] = batch_index + 1
                if state["global_step"] % 25 == 0 or batch_index == 0:
                    avg = state["running_loss_sum"] / state["running_tokens"]
                    status("training", current_train_loss=avg, lr=lr,
                           gpu_peak_mb=round(torch.cuda.max_memory_allocated() / 1024**2, 1) if use_amp else None)
                    print(f"epoch={epoch} batch={batch_index + 1}/{batches_per_epoch} step={state['global_step']} train_loss={avg:.5f}", flush=True)
                if state["global_step"] % args.save_every == 0:
                    save_checkpoint(args.run / "last.pt")
            status("validating")
            train_loss = state["running_loss_sum"] / max(1, state["running_tokens"])
            val_loss = evaluate("validation")
            if val_loss is None or not math.isfinite(val_loss):
                raise RuntimeError("Invalid validation loss")
            improved = state["best_val_loss"] is None or val_loss < state["best_val_loss"] - 0.0001
            if improved:
                state["best_val_loss"], state["best_epoch"], state["bad_epochs"] = val_loss, epoch, 0
            else:
                state["bad_epochs"] += 1
            record = {"epoch": epoch, "train_loss": train_loss, "val_loss": val_loss,
                      "seconds": round(time.monotonic() - epoch_started, 2), "global_step": state["global_step"], "best": improved}
            state["history"].append(record)
            state["epoch_completed"] = True
            state["next_batch"] = 0
            state["running_loss_sum"] = 0.0
            state["running_tokens"] = 0
            atomic_json(args.run / "history.json", state["history"])
            save_checkpoint(args.run / "last.pt")
            if improved:
                save_checkpoint(args.run / "gmd_transformer_pretrained.pt")
            status("epoch_complete", train_loss=train_loss, val_loss=val_loss, early_stop_counter=state["bad_epochs"])
            print(json.dumps({"event": "epoch_complete", **record}), flush=True)
            if state["bad_epochs"] >= args.patience:
                print(f"Early stopping after {state['bad_epochs']} epochs without validation improvement", flush=True)
                break
            state["epoch_completed"] = False
        best = torch.load(args.run / "gmd_transformer_pretrained.pt", map_location="cpu", weights_only=True)
        model.load_state_dict(best["model_state_dict"])
        status("testing_best_checkpoint")
        test_loss = evaluate("test")
        summary = {"status": "completed", "completed_at": utc_now(), "best_epoch": state["best_epoch"],
            "best_val_loss": state["best_val_loss"], "test_loss": test_loss,
            "epochs_completed": len(state["history"]), "training_seconds_this_process": round(time.monotonic() - start_time, 1),
            "checkpoint": str(args.run / "gmd_transformer_pretrained.pt"), "resumable_checkpoint": str(args.run / "last.pt"),
            "smoke_test": bool(args.max_batches), "data": metadata, "model": asdict(config), "machine": machine}
        if not args.no_samples:
            status("generating_sample", test_loss=test_loss)
            try:
                from .generate import generate_files
                generate_files(model, vocab, args.run / "sample", bars=8, seed=123, device=device)
                summary["sample"] = str(args.run / "sample.mid")
            except Exception as error:
                summary["sample_error"] = str(error)
                (args.run / "sample-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        atomic_json(args.run / "summary.json", summary)
        status("completed", test_loss=test_loss, summary=str(args.run / "summary.json"))
        print(json.dumps({"event": "completed", **summary}, ensure_ascii=False), flush=True)
        return summary
    except BaseException as error:
        status("interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=f"{type(error).__name__}: {error}")
        (args.run / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise


def deepcopy_state(state):
    # State consists entirely of JSON-compatible numbers, strings, lists and dicts.
    return json.loads(json.dumps(state))


def main():
    args = parser().parse_args()
    keep_awake = os.name == "nt"
    try:
        if keep_awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        train(args)
    except BaseException:
        if args.run.exists() and not (args.run / "error.txt").exists():
            (args.run / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if keep_awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == "__main__":
    main()
