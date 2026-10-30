"""比較專案、固定輸入快照與逐次保存的實驗紀錄。"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil

from guoyue_annotator.photo_assets import asset_file_path, safe_project_path
from score_recognizer.contract import ContractError, FORMAT, SCHEMA_VERSION
from score_recognizer.geometry import region_point, quad_bounds
from score_recognizer.provider import build_request
from score_recognizer.workspace import Workspace, MANIFEST as SOURCE_MANIFEST, atomic_json, identity, now, validate_region

MANIFEST = "comparison-project.json"
MODELS = ("gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")


def model_names(models):
    names = list(dict.fromkeys(m.strip() for m in models if m.strip()))
    if len(names) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_.:/-]+", m) for m in names):
        raise ContractError("請選至少兩個不同的模型；模型名稱不可包含空白")
    return names


def new_run(model):
    return {"run_id": identity("CMP"), "model": model, "status": "queued", "usage": None,
            "result": None, "raw_output": None, "error": None, "elapsed_seconds": None, "cost_usd": None}


def single_measure(region, index):
    """裁出單小節，沿用透視座標；原整範圍可用作參考。"""
    if not 0 <= index < region["bar_count"]:
        raise ContractError("所選小節不在這個範圍內")
    sub = deepcopy(region)
    left, right = region["boundaries"][index:index + 2]
    crop_left = 0 if index == 0 else left
    sub["quad"] = [region_point(region, u, v) for u, v in
                   ((crop_left, 0), (right, 0), (right, 1), (crop_left, 1))]
    sub.update(bbox=quad_bounds(sub["quad"]), start_bar=region["start_bar"] + index,
               bar_count=1, boundaries=[(left - crop_left) / (right - crop_left), 1],
               context_bbox=region.get("context_bbox") or (region["bbox"] if region["bar_count"] > 1 else None))
    validate_region(sub)
    return sub


class ComparisonProject:
    def __init__(self, root, data, workspace):
        self.root, self.data, self.workspace = Path(root).resolve(), data, workspace

    @classmethod
    def create(cls, root, title, *, _publish=True):
        root = Path(root).resolve()
        if root.exists():
            raise ContractError("比較專案資料夾已存在，請改名或使用『開啟比較專案』")
        if not title.strip():
            raise ContractError("請輸入比較專案名稱")
        root.mkdir(parents=True)
        workspace = Workspace.create(root / "source", title.strip())
        data = {"format": "musicai-model-comparison", "schema_version": "1.0", "title": title.strip(),
                "created_at": now(), "updated_at": now(), "source_origin": None, "cases": [],
                "settings": {"models": list(MODELS[:2]), "image_detail": "high", "reasoning": "medium"}}
        project = cls(root, data, workspace)
        if _publish:
            project.save()
        return project

    @classmethod
    def copy_source(cls, source, root, title=None):
        """只讀原專案 JSON；絕不呼叫會復原 running 狀態的 Workspace.load。"""
        source = Path(source).resolve()
        if source.is_dir():
            source /= SOURCE_MANIFEST
        package = json.loads(source.read_text(encoding="utf-8"))
        if package.get("format") != FORMAT or package.get("schema_version") != SCHEMA_VERSION:
            raise ContractError("請選程式 A 的 recognition-package.json")
        if Path(root).resolve().is_relative_to(source.parent):
            raise ContractError("比較專案請存放在原辨識專案以外的獨立資料夾")
        files = [(asset, asset_file_path(source.parent, asset, must_exist=True)) for asset in package["assets"]]
        for region in package["regions"]:
            validate_region(region)
        project = cls.create(root, title or package["title"] + " 模型比較", _publish=False)
        copied = deepcopy(package)
        copied.update(package_id=identity("PKG"), recognition_runs=[], navigation_reviews=[], revision=1)
        for asset, path in files:
            destination = safe_project_path(project.workspace.root, asset["relative_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            if sha256(destination.read_bytes()).hexdigest() != asset["sha256"]:
                raise ContractError("原圖內容已變更，與專案記錄的照片不符；未修改原專案")
        project.workspace.package = copied
        project.workspace.save()
        project.data["source_origin"] = {"path": str(source), "package_id": package["package_id"], "copied_at": now()}
        project.save()
        return project

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        if path.is_dir():
            path /= MANIFEST
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != "musicai-model-comparison" or data.get("schema_version") != "1.0":
            raise ContractError("這不是比較專案；要使用 A 的範圍請按『複製 A 的照片與範圍』")
        source = (path.parent / "source").resolve()
        if not source.is_relative_to(path.parent):
            raise ContractError("比較專案的照片與範圍必須存放在比較資料夾內")
        project = cls(path.parent, data, Workspace.load(source))
        changed = False
        for case in data["cases"]:
            for run in case["runs"]:
                if run["status"] == "running":
                    run.update(status="interrupted", error="上次工作中斷；不會自動重送，伺服器可能已處理該請求")
                    changed = True
                elif run["status"] == "queued":
                    run.update(status="cancelled", error="上次排程尚未送出；不會自動重送")
                    changed = True
        if changed:
            project.save()
        return project

    def save(self):
        self.data["updated_at"] = now()
        atomic_json(self.root / MANIFEST, self.data)

    def case(self, case_id):
        return next(c for c in self.data["cases"] if c["case_id"] == case_id)

    def prepare(self, selections, models, detail="high", reasoning="medium"):
        """送出前一次建立固定輸入；每個模型只替換 model，避免互看答案。"""
        models = model_names(models)
        if detail not in {"low", "high", "original", "auto"}:
            raise ContractError("圖片細節設定無效")
        if reasoning not in {"default", "low", "medium", "high", "xhigh", "max"}:
            raise ContractError("推理設定無效")
        selections = list(dict.fromkeys(selections))
        if not selections:
            raise ContractError("請選擇至少一個小節")
        # 所有選取先檢查，避免半途才發現某範圍尚未確認。
        for region_id, index in selections:
            region = self.workspace.region(region_id)
            if not region["confirmed"]:
                raise ContractError("請先確認所選範圍與小節分界")
            single_measure(region, index)
        created = []
        batch_id = identity("BATCH")
        for region_id, index in selections:
            region = self.workspace.region(region_id)
            sub = single_measure(region, index)
            request = build_request(self.workspace, sub, model=models[0], detail=detail)
            if reasoning != "default":
                request["reasoning"] = {"effort": reasoning}
            request.pop("model")
            digest = sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            case_id = identity("CASE")
            folder = self.root / "cases" / case_id
            folder.mkdir(parents=True)
            atomic_json(folder / "request.json", request)
            import base64
            images = [i for i in request["input"][0]["content"] if i["type"] == "input_image"]
            for number, image in enumerate(images):
                (folder / ("primary.png" if number == 0 else "context.png")).write_bytes(base64.b64decode(image["image_url"].split(",", 1)[1]))
            case = {"case_id": case_id, "batch_id": batch_id, "created_at": now(), "bar": sub["start_bar"],
                    "page": self.workspace.asset(region["asset_id"])["printed_page_number"],
                    "region_id": region_id, "region_fingerprint": self.workspace.fingerprint(region),
                    "source_snapshot": sub, "input_sha256": digest,
                    "request_file": f"cases/{case_id}/request.json", "image_detail": detail,
                    "reasoning": reasoning, "judgment": None,
                    "runs": [new_run(m) for m in models]}
            created.append(case)
        self.data["cases"].extend(created)
        self.data["settings"] = {"models": models, "image_detail": detail, "reasoning": reasoning}
        self.save()
        return created

    def retry_failed(self, case_id):
        case = self.case(case_id)
        selected = [r for r in case["runs"] if r["status"] in {"failed", "invalid", "interrupted", "cancelled", "empty"}]
        if not selected:
            raise ContractError("這筆比較沒有失敗或中斷的模型；要全部重做請新增一輪比較")
        # 先驗證固定輸入，再封存舊次用量；成功模型不重送。
        self.request_for(case, selected[0]["model"])
        for run in selected:
            case.setdefault("previous_attempts", []).append(deepcopy(run))
            replacement = new_run(run["model"])
            run.clear()
            run.update(replacement)
        case["judgment"] = None
        self.save()
        return case, len(selected)

    def request_for(self, case, model):
        request = json.loads(safe_project_path(self.root, case["request_file"], must_exist=True).read_text(encoding="utf-8"))
        digest = sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if digest != case["input_sha256"]:
            raise ContractError("固定輸入檔已改變，請建立新一輪比較")
        request["model"] = model
        return request
