"""Compare identity-preserving and GMD-projected fine-tuning on aligned targets.

Instrument probabilities are summed into common GMD classes; all other tokens
form an invalid-instrument class. This avoids comparing 32-way vs 11-way accuracy.
"""
import argparse
from collections import defaultdict
from contextlib import nullcontext
from functools import partial
import json
from pathlib import Path

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

from .data import TokenDataset, collate, file_hash, load_data
from .evaluate_finetune import category
from .model import ModelConfig, RhythmTransformer
from .project_gmd12 import TARGET_LABELS


def instrument_groups(vocab, token_map):
    targets = [f"<STROKE_{key}>" for key in TARGET_LABELS]
    classes = targets + ["INVALID_INSTRUMENT_TOKEN"]
    ids = [[] for _ in classes]
    token_classes = {}
    for token, token_id in vocab.items():
        target = token_map.get(token, token)
        group = classes.index(target) if target in targets else len(targets)
        ids[group].append(token_id)
        token_classes[token_id] = group
    return classes, ids, token_classes


@torch.inference_mode()
def metrics(checkpoint_path, data_dir, mapping, device="cuda"):
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    vocab, splits, meta = load_data(data_dir / "sequences.jsonl", data_dir / "vocab.json")
    if checkpoint["vocab"] != vocab:
        raise ValueError("Checkpoint vocabulary differs")
    model = RhythmTransformer(ModelConfig(**checkpoint["model_config"])).to(device).eval()
    model.load_state_dict(checkpoint["model_state_dict"])
    itos = {value:key for key,value in vocab.items()}
    classes, ids, token_classes = instrument_groups(vocab, mapping)
    group_ids = [torch.tensor(group, device=device) for group in ids]
    loader = DataLoader(TokenDataset(splits["validation"], return_mask=True), batch_size=8,
                        collate_fn=partial(collate, pad_id=0))
    stats = defaultdict(lambda: {"count":0, "loss_sum":0., "correct":0})
    coarse_loss, coarse_correct, coarse_count = 0., 0, 0
    dtype = torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float16
    for x,y,mask in loader:
        x,y,mask = x.to(device),y.to(device),mask.to(device)
        with torch.autocast("cuda",dtype=dtype) if device == "cuda" else nullcontext():
            logits = model(x)
            loss = F.cross_entropy(logits.flatten(0,1),y.flatten(),reduction="none").view_as(y)
        valid = mask & y.ne(0)
        raw = logits.argmax(-1)
        targets = y[valid].cpu().tolist()
        predictions = raw[valid].cpu().tolist()
        losses = loss[valid].cpu().tolist()
        instrument_positions = []
        for position,(target,prediction,value) in enumerate(zip(targets,predictions,losses)):
            field = category(itos[target])
            stats[field]["count"] += 1
            stats[field]["loss_sum"] += value
            stats[field]["correct"] += int(target==prediction)
            if field == "instrument":
                instrument_positions.append(position)
        if instrument_positions:
            position_tensor = torch.tensor(instrument_positions, device=device)
            probs = F.log_softmax(logits[valid].float()[position_tensor],dim=-1)
            grouped = torch.stack([torch.logsumexp(probs[:,group],dim=-1) for group in group_ids],dim=-1)
            group_targets = torch.tensor([token_classes[targets[p]] for p in instrument_positions],device=device)
            coarse_loss += float(-grouped.gather(1,group_targets[:,None]).sum())
            coarse_correct += int((grouped.argmax(-1)==group_targets).sum())
            coarse_count += len(instrument_positions)
    count = sum(v["count"] for v in stats.values())
    return {"checkpoint":str(checkpoint_path.resolve()), "checkpoint_sha256":file_hash(checkpoint_path),
        "epoch":checkpoint["epoch"], "known_targets":count,
        "validation_loss":sum(v["loss_sum"] for v in stats.values())/count,
        "fields":{name:{"count":v["count"],"loss":v["loss_sum"]/v["count"],"top1_accuracy":v["correct"]/v["count"]} for name,v in stats.items()},
        "common_gmd12_instruments":{"count":coarse_count,"loss":coarse_loss/coarse_count,
            "top1_accuracy":coarse_correct/coarse_count,
            "definition":"Sum probabilities of all original INST labels and native STROKE tokens that map to the same GMD class; other tokens are one invalid class."}}


def compare(control_checkpoint, mapped_checkpoint, control_data, mapped_data, output, device="cuda"):
    torch.set_num_threads(4)
    mapping_data = json.loads((mapped_data/"instrument_mapping.json").read_text(encoding="utf-8"))
    token_map = mapping_data["token_map"]
    control_rows = [json.loads(line) for line in (control_data/"sequences.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    mapped_rows = [json.loads(line) for line in (mapped_data/"sequences.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(control_rows) != len(mapped_rows):
        raise ValueError("Comparison rows differ")
    for old,new in zip(control_rows,mapped_rows):
        if old["segment_id"]!=new["segment_id"] or old["split"]!=new["split"] or old["loss_mask"]!=new["loss_mask"]:
            raise ValueError("Split, sequence or mask alignment differs")
        if new["tokens"] != [token_map.get(token,token) for token in old["tokens"]]:
            raise ValueError("A noninstrument token changed")
    first=torch.load(control_checkpoint,map_location="cpu",weights_only=True)
    second=torch.load(mapped_checkpoint,map_location="cpu",weights_only=True)
    if first["model_config"]!=second["model_config"] or first["vocab"]!=second["vocab"]:
        raise ValueError("Model size or vocabulary IDs differ")
    settings=("epochs","batch_size","lr","seed","patience","warmup","save_every","device","init_from")
    for key in settings:
        if first["training_args"][key]!=second["training_args"][key]:
            raise ValueError(f"Training setting differs: {key}")
    del first,second
    a=metrics(control_checkpoint,control_data,token_map,device)
    b=metrics(mapped_checkpoint,mapped_data,token_map,device)
    result={"experiment":"Original 32 instruments vs functional GMD12 projection",
        "control":a,"mapped":b,"same_rows_masks_vocabulary_architecture_and_training_settings":True,
        "common_instrument_accuracy_change_pp":100*(b["common_gmd12_instruments"]["top1_accuracy"]-a["common_gmd12_instruments"]["top1_accuracy"]),
        "interpretation":["Raw instrument and overall losses have different target granularity and are not alone evidence of better music.",
            "Common GMD12 instrument scores aggregate probabilities on identical coarse labels, including an invalid-token class.",
            "Time/duration/other field targets are unchanged, evaluated on corresponding validation contexts.",
            "Single seed and one held-out work; no free-generation/listening quality evaluation."]}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--control-checkpoint",type=Path,required=True)
    p.add_argument("--mapped-checkpoint",type=Path,required=True)
    p.add_argument("--control-data",type=Path,required=True)
    p.add_argument("--mapped-data",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--device",choices=("cpu","cuda"),default="cuda")
    args=p.parse_args()
    result=compare(args.control_checkpoint,args.mapped_checkpoint,args.control_data,args.mapped_data,args.output,args.device)
    print(json.dumps({"control_loss":result['control']['validation_loss'],"mapped_loss":result['mapped']['validation_loss'],
        "control_common_instrument":result['control']['common_gmd12_instruments'],
        "mapped_common_instrument":result['mapped']['common_gmd12_instruments'],
        "mapped_fields":result['mapped']['fields']},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
