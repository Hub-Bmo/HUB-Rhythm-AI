"""Portable entry point. Archived inputs/models are read-only experiment records."""
from __future__ import annotations

import argparse
from datetime import datetime
import gzip
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SIX = ROOT / "outputs/六份樂譜_20261029_TOKEN版"
TOKEN_PACKAGE = SIX / "TOKEN/tokens-20261029-113446-d35a4b"


def digest(path):
    result = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(command, cwd=ROOT, log=None):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    temp = ROOT / "work/tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = str(temp)
    print("Running:", " ".join(map(str, command)), flush=True)
    if log:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as stream:
            subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    else:
        subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def doctor():
    result = {"python":sys.version,"executable":sys.executable,"root":str(ROOT),"packages":{}}
    for name in ("torch","numpy","mido","pandas"):
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = None
    try:
        import torch
        result.update({"cuda_available":torch.cuda.is_available(),"cuda_build":torch.version.cuda,
                       "gpu":torch.cuda.get_device_name(0) if torch.cuda.is_available() else None})
    except ImportError:
        result["cuda_available"] = False
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def verify(models=False):
    manifest = json.loads((ROOT / "package_manifest.json").read_text(encoding="utf-8"))
    missing, changed = [], []
    for item in manifest["files"]:
        path = ROOT / item["path"]
        if not path.is_file():
            missing.append(item["path"])
        elif path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            changed.append(item["path"])
    result = {"checked_files":len(manifest["files"]),"missing":missing,"changed":changed}
    if missing or changed:
        save_json(ROOT / "verification/integrity.json",result)
        raise ValueError("Integrity check failed: " + json.dumps(result,ensure_ascii=False))
    if models:
        import torch
        from rhythm_transformer.model import ModelConfig, RhythmTransformer
        from rhythm_transformer.data import load_data
        torch.set_num_threads(2)
        result["models"] = []
        for key,spec in experiments().items():
            cp_path = ROOT / spec["checkpoint"]
            checkpoint = torch.load(cp_path,map_location="cpu",weights_only=True)
            if checkpoint["architecture"] != "gmd-causal-transformer-v1":
                raise ValueError("Unexpected checkpoint architecture")
            data_dir = ROOT / spec["data_dir"]
            vocab,splits,meta = load_data(data_dir / spec["sequence_file"],data_dir / "vocab.json")
            if vocab != checkpoint["vocab"] or meta["sequence_sha256"] != checkpoint["data_metadata"]["sequence_sha256"]:
                raise ValueError(f"Checkpoint/data mismatch: {key}")
            model = RhythmTransformer(ModelConfig(**checkpoint["model_config"])).eval()
            model.load_state_dict(checkpoint["model_state_dict"])
            with torch.inference_mode():
                logits = model(torch.tensor([[vocab["<BOS>"],vocab["<HIT_SET>"]]]))
            if not torch.isfinite(logits).all():
                raise ValueError("Non-finite model output")
            result["models"].append({"version":key,"vocab_size":len(vocab),"epoch":checkpoint["epoch"],
                                     "best_val_loss":checkpoint["best_val_loss"],"loaded":True})
            del model,checkpoint
    result["passed"] = True
    save_json(ROOT / "verification/integrity.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def experiments():
    return json.loads((ROOT / "experiments.json").read_text(encoding="utf-8"))["experiments"]


def compare_sequences(actual,expected,fields):
    rows = lambda p:[json.loads(line) for line in Path(p).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    left,right = rows(actual),rows(expected)
    if len(left) != len(right):
        raise ValueError(f"Sequence count differs: {len(left)} vs {len(right)}")
    differences = []
    for index,(a,b) in enumerate(zip(left,right)):
        for field in fields:
            if a.get(field) != b.get(field):
                differences.append({"row":index,"field":field})
                if len(differences) >= 12:
                    break
        if len(differences) >= 12:
            break
    result = {"rows":len(left),"semantic_equal":not differences,"differences":differences,
              "byte_equal":digest(actual)==digest(expected),"actual_sha256":digest(actual),"expected_sha256":digest(expected)}
    if differences:
        raise ValueError("Regenerated data differs: " + json.dumps(result))
    return result


def rebuild(stage="all",from_midi=False):
    base = ROOT / "work" / ("rebuild-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    base.mkdir(parents=True,exist_ok=False)
    results = {"work_dir":str(base),"stages":{}}
    selected = ("gmd","guoyue","gmd12") if stage == "all" else (stage,)
    if "gmd" in selected:
        gmd = base / "gmd"
        gmd.mkdir()
        if from_midi:
            shutil.copytree(ROOT / "data/gmd/raw_midi",gmd / "groove")
            run([sys.executable,ROOT / "legacy/gmd_preprocessing/convert_gmd_to_csv.py"],cwd=gmd,log=gmd / "01-midi-convert.log")
        else:
            with gzip.open(ROOT / "data/gmd/intermediate/gmd_events.csv.gz","rb") as src,(gmd / "gmd_events.csv").open("wb") as dst:
                shutil.copyfileobj(src,dst)
        run([sys.executable,ROOT / "legacy/gmd_preprocessing/events_to_tokens.py"],cwd=gmd,log=gmd / "02-tokenize.log")
        run([sys.executable,ROOT / "legacy/gmd_preprocessing/01_build_vocab.py"],cwd=gmd,log=gmd / "03-vocab.log")
        expected = ROOT / "projects/gmd_transformer_v1/data"
        results["stages"]["gmd"] = compare_sequences(gmd / "gmd_sequences_v2.jsonl",expected / "gmd_sequences_v2.jsonl",
            ("tokens","segment_id","split","midi_filename","bpm","time_signature","event_count","hit_set_count"))
        if json.loads((gmd / "vocab.json").read_text(encoding="utf-8-sig")) != json.loads((expected / "vocab.json").read_text(encoding="utf-8-sig")):
            raise ValueError("Rebuilt GMD vocabulary IDs differ")
    if "guoyue" in selected or "gmd12" in selected:
        from score_tokenizer.core import Store
        from rhythm_transformer.prepare_finetune import prepare
        library = base / "tokenizer_library"
        library.mkdir()
        shutil.copyfile(SIX / "TOKEN工作庫/tokenizer-library.json",library / "tokenizer-library.json")
        store = Store(library)
        for piece in store.data["pieces"].values():
            raw_json = SIX / "訓練JSON" / (piece["title"] + ".json")
            payload = json.loads(raw_json.read_text(encoding="utf-8-sig"))
            if payload != piece["payload"]:
                raise ValueError(f"Reviewed JSON and library snapshot differ: {piece['title']}")
            piece["payload"] = payload
        folder,manifest = store.export(list(store.data["pieces"]),base / "guoyue_tokens")
        result = compare_sequences(folder / "sequences.jsonl",TOKEN_PACKAGE / "sequences.jsonl",
            ("tokens","token_ids","loss_mask","piece_id","segment_id","source_bars","total_ticks","timeline","provenance"))
        data_dir = base / "fine_tune_data"
        prepare(folder,ROOT / "projects/gmd_transformer_v1/data/vocab.json",data_dir)
        fine = compare_sequences(data_dir / "sequences.jsonl",ROOT / "projects/guoyue_transformer_ft_v1/data/sequences.jsonl",
            ("tokens","token_ids","loss_mask","piece_id","segment_id","split","source_bars","total_ticks","timeline","provenance"))
        results["stages"]["guoyue"] = {"tokenization":result,"fine_tune_alignment":fine}
        if "gmd12" in selected:
            from rhythm_transformer.project_gmd12 import project
            mapped = base / "gmd12_data"
            project(data_dir,folder,mapped)
            results["stages"]["gmd12"] = compare_sequences(mapped / "sequences.jsonl",ROOT / "outputs/guoyue_gmd12_tokens_v1/sequences.jsonl",
                ("tokens","token_ids","loss_mask","piece_id","segment_id","split","source_bars","total_ticks","timeline","provenance"))
    save_json(base / "rebuild_report.json",results)
    save_json(ROOT / "verification/latest_rebuild.json",results)
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return results


def selected_device(value):
    if value != "auto":
        return value
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def training_command(key,spec,run_dir,device,base,epochs=None,smoke=False,resume=False):
    data_dir = ROOT / spec["data_dir"]
    settings = spec["settings"]
    command = [sys.executable,"-u","-m","rhythm_transformer.train","--data",data_dir / spec["sequence_file"],
        "--vocab",data_dir / "vocab.json","--run",run_dir,"--epochs",str(epochs or (1 if smoke else settings["epochs"])),
        "--batch-size",str(settings["batch_size"]),"--lr",str(settings["lr"]),"--seed",str(settings["seed"]),
        "--warmup",str(settings["warmup"]),"--patience",str(settings["patience"]),"--save-every",str(settings["save_every"]),
        "--device",device,"--model-name",spec["model_name"]]
    if key != "v1" or smoke:
        command.append("--no-samples")
    if smoke:
        command += ["--max-batches","2"]
    if resume:
        command += ["--resume",run_dir / "last.pt"]
    elif key != "v1":
        command += ["--init-from",base]
    return command


def train(args):
    if Path(args.run_name).name != args.run_name or args.run_name in {".",".."}:
        raise ValueError("run-name must be a single folder name")
    specs = experiments()
    keys = ("v1","v2","v3") if args.stage == "all" else (args.stage,)
    base_dir = ROOT / "reproduced_runs" / args.run_name
    device = selected_device(args.device)
    base = ROOT / specs["v1"]["checkpoint"]
    for key in keys:
        directory = base_dir / key
        if args.resume and not (directory / "last.pt").exists():
            raise ValueError(f"No resumable checkpoint at {directory}")
        directory.mkdir(parents=True,exist_ok=True)
        command = training_command(key,specs[key],directory,device,base,args.epochs,args.smoke,args.resume)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run(command,log=directory / ("console-"+stamp+".log"))
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        print(key,json.dumps({k:summary[k] for k in ("best_epoch","best_val_loss","smoke_test")}),flush=True)
        if key == "v1":
            base = directory / specs[key]["model_name"]
    save_json(base_dir / "reproduction_request.json",{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()})


def evaluate():
    from rhythm_transformer.evaluate_finetune import evaluate as evaluate_one
    from rhythm_transformer.compare_gmd12 import compare
    specs = experiments()
    for key in ("v2","v3"):
        spec = specs[key]
        evaluate_one(ROOT / spec["checkpoint"],ROOT / spec["data_dir"],selected_device("auto"))
    result = compare(ROOT / specs["v2"]["checkpoint"],ROOT / specs["v3"]["checkpoint"],
        ROOT / specs["v2"]["data_dir"],ROOT / specs["v3"]["data_dir"],
        ROOT / "verification/current_comparison.json",selected_device("auto"))
    print(json.dumps({"v2_loss":result["control"]["validation_loss"],"v3_loss":result["mapped"]["validation_loss"]},indent=2))


def main():
    p = argparse.ArgumentParser(description="MusicAI portable research reproduction")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor")
    v = sub.add_parser("verify")
    v.add_argument("--models",action="store_true")
    r = sub.add_parser("rebuild-data")
    r.add_argument("--stage",choices=("all","gmd","guoyue","gmd12"),default="all")
    r.add_argument("--from-midi",action="store_true")
    t = sub.add_parser("train")
    t.add_argument("--stage",choices=("all","v1","v2","v3"),default="all")
    t.add_argument("--device",choices=("auto","cuda","cpu"),default="auto")
    t.add_argument("--run-name",default=datetime.now().strftime("run-%Y%m%d-%H%M%S"))
    t.add_argument("--epochs",type=int)
    t.add_argument("--smoke",action="store_true")
    t.add_argument("--resume",action="store_true")
    sub.add_parser("evaluate")
    sub.add_parser("test")
    args = p.parse_args()
    os.chdir(ROOT)
    if args.command == "doctor": doctor()
    elif args.command == "verify": verify(args.models)
    elif args.command == "rebuild-data": rebuild(args.stage,args.from_midi)
    elif args.command == "train": train(args)
    elif args.command == "evaluate": evaluate()
    elif args.command == "test":
        (ROOT / ".verification-temp").mkdir(exist_ok=True)
        run([sys.executable,"-m","unittest","tests.test_rhythm_transformer","tests.test_gmd12_projection","tests.test_score_tokenizer","-v"])


if __name__ == "__main__":
    main()
