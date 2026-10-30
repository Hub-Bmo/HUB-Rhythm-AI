"""以頁序修復 A/B 小節索引；只重標號，不重新辨識或推測小節數。"""
from copy import deepcopy
from pathlib import Path
import json
import re
from hashlib import sha256

from .contract import ContractError
from .workspace import Workspace, MANIFEST, atomic_bytes, atomic_json, identity, now


def plan_numbering(package):
    assets = sorted(package["assets"], key=lambda a: a["page_sequence"])
    sequences = [a["page_sequence"] for a in assets]
    if len(sequences) != len(set(sequences)):
        raise ContractError("頁序重複，請先確認照片順序")
    unknown = {r["asset_id"] for r in package["regions"]} - {a["asset_id"] for a in assets}
    if unknown:
        raise ContractError("辨識範圍缺少來源照片")
    mapping, regions, pages = {}, [], []
    number = 1
    for asset in assets:
        source = sorted((r for r in package["regions"] if r["asset_id"] == asset["asset_id"]), key=lambda r: r["start_bar"])
        if not source:
            raise ContractError(f"{asset['original_filename']} 尚未登記小節，無法推測後續起點")
        start = number
        old_numbers = []
        for region in source:
            old, count = region["start_bar"], region["bar_count"]
            regions.append({"region_id": region["region_id"], "asset_id": asset["asset_id"],
                            "old_start": old, "new_start": number, "count": count})
            for index in range(count):
                if old + index in mapping:
                    raise ContractError("原登記小節重疊，不能安全重新編號")
                mapping[old + index] = number + index
                old_numbers.append(old + index)
            number += count
        pages.append({"file": asset["original_filename"], "page": asset["printed_page_number"],
                      "old_start": min(old_numbers), "old_end": max(old_numbers), "new_start": start, "new_end": number - 1})
    if not mapping:
        raise ContractError("沒有已登記小節")
    return {"mapping": mapping, "regions": regions, "pages": pages,
            "changed_bars": sum(k != v for k, v in mapping.items()), "total_bars": len(mapping)}


def _context_number(text, old, new):
    # 只更新程式拆分時自動加入的提示，原稿文字與使用者備註不做數字替換。
    return re.sub(r"(?m)(^僅辨識小節 )" + str(old) + r"(?=；參考圖提供)",
                  lambda m: m[1] + str(new), text or "")


def repair_pair(package, review):
    if review["source_package_id"] != package["package_id"]:
        raise ContractError("辨識與校對專案不是同一首來源")
    if any(r["status"] == "running" for r in package["recognition_runs"]):
        raise ContractError("仍有辨識工作執行中，請等回應保存後再改號")
    # 區間跨頁重排後未必仍連續；有人工曲式設定時不能只改起訖兩個數字。
    if any(review.get(k) for k in ("segments", "navigation", "navigation_draft", "navigation_scopes")) or package.get("navigation_reviews"):
        raise ContractError("已有人工段落或反覆範圍，必須另作順序遷移；本工具不會刪除或猜測這些設定")
    plan = plan_numbering(package)
    mapping = plan["mapping"]
    if set(map(int, review["bars"])) != set(mapping):
        raise ContractError("A/B 登記的小節集合不一致，先檢查來源匯入狀態")
    region_by_id = {r["region_id"]: r for r in package["regions"]}
    for bar in review["bars"].values():
        region = region_by_id.get(bar["region_id"])
        if not region or bar["asset_id"] != region["asset_id"] or not region["start_bar"] <= bar["number"] < region["start_bar"] + region["bar_count"]:
            raise ContractError("校對小節的來源範圍不一致，不能只按數字搬移")
    if not plan["changed_bars"]:
        return deepcopy(package), deepcopy(review), plan

    source, corrected = deepcopy(package), deepcopy(review)
    migration_id = identity("NUMBERING")
    stamp = now()
    old_workspace, new_workspace = Workspace(Path("."), package), Workspace(Path("."), source)
    old_fingerprints = {r["region_id"]: old_workspace.fingerprint(r) for r in package["regions"]}
    new_regions = {r["region_id"]: r for r in source["regions"]}
    changed_regions = {}
    for item in plan["regions"]:
        old, new = item["old_start"], item["new_start"]
        if old == new:
            continue
        region = new_regions[item["region_id"]]
        region.update(start_bar=new, numbering_revision=migration_id,
                      context_notes=_context_number(region.get("context_notes"), old, new))
        changed_regions[item["region_id"]] = item

    def mapped(value):
        if value not in mapping:
            raise ContractError(f"小節 {value} 未登記，無法安全改號")
        return mapping[value]

    def marker_numbers(marker):
        for field in ("start_bar", "end_bar"):
            if marker.get(field) is not None:
                marker[field] = mapped(marker[field])
        if marker.get("end_bar") is not None and marker["end_bar"] < marker["start_bar"]:
            raise ContractError("反覆候選跨越重排頁面，必須另作順序核對")

    adjusted_runs = 0
    for run in source["recognition_runs"]:
        item = changed_regions.get(run["region_id"])
        if not item or run["fingerprint"] != old_fingerprints[run["region_id"]]:
            continue  # 非現行來源的歷史結果照原樣保存，不把它誤認成現行結果。
        snapshot = run["source_snapshot"]
        run.setdefault("numbering_history", []).append({"migration_id": migration_id, "at": stamp,
            "source_snapshot": deepcopy(snapshot), "fingerprint": run["fingerprint"],
            "navigation": deepcopy((run.get("result") or {}).get("navigation", []))})
        snapshot.update(start_bar=item["new_start"], numbering_revision=migration_id,
                        context_notes=_context_number(snapshot.get("context_notes"), item["old_start"], item["new_start"]))
        run["fingerprint"] = new_workspace.fingerprint(new_regions[run["region_id"]])
        for observation in run.get("observations", []):
            if not item["old_start"] <= observation["bar"] < item["old_start"] + item["count"]:
                raise ContractError("辨識事件超出其來源範圍，不能直接重標號")
            observation["bar"] = mapped(observation["bar"])
        for marker in (run.get("result") or {}).get("navigation", []):
            marker_numbers(marker)
        adjusted_runs += 1

    corrected["bars"] = {str(mapped(int(key))): {**deepcopy(value), "number": mapped(int(key))}
                         for key, value in review["bars"].items()}
    corrected["bars"] = dict(sorted(corrected["bars"].items(), key=lambda pair: int(pair[0])))
    for region_id, value in corrected["regions"].items():
        if region_id in changed_regions:
            value["start_bar"] = changed_regions[region_id]["new_start"]
    for entry in corrected["entries"]:
        entry["value"]["bar"] = mapped(entry["value"]["bar"])
    # original / archived_entries / raw_output 都是歷史證據，保留辨識當時編號。
    for candidate in corrected["navigation_candidates"]:
        marker_numbers(candidate["marker"])
    corrected["navigation_draft_scope"] = None
    record = {"migration_id": migration_id, "at": stamp, "reason": "依來源頁序重排已登記小節；頁內順序與小節數不變",
              "mapping": {str(k): v for k, v in mapping.items()}, "pages": deepcopy(plan["pages"])}
    source.setdefault("numbering_history", []).append(deepcopy(record))
    corrected.setdefault("numbering_history", []).append(deepcopy(record))
    source.update(revision=source["revision"] + 1, updated_at=stamp)
    corrected["updated_at"] = stamp
    plan["adjusted_runs"] = adjusted_runs
    return source, corrected, plan


def repair_files(source_path, review_path, backup_dir, *, apply=False):
    """呼叫前須關閉 A/B；核對雜湊、先備份，第二份寫入失敗時回復第一份。"""
    source_path, review_path, backup_dir = Path(source_path).resolve(), Path(review_path).resolve(), Path(backup_dir).resolve()
    originals = {p: p.read_bytes() for p in (source_path, review_path)}
    package, review = (json.loads(originals[p].decode("utf-8")) for p in (source_path, review_path))
    source, corrected, plan = repair_pair(package, review)
    if not apply or not plan["changed_bars"]:
        return plan
    if backup_dir.exists():
        raise ContractError("備份資料夾已存在，請使用新位置")
    backup_dir.mkdir(parents=True)
    for path, payload in originals.items():
        name = "recognition-package.json" if path == source_path else "review-project.json"
        atomic_bytes(backup_dir / name, payload)
    atomic_json(backup_dir / "numbering-map.json", plan)
    atomic_json(backup_dir / "recovery.json", {"files": [{"path": str(p), "sha256": sha256(v).hexdigest()}
                                                        for p, v in originals.items()]})
    for path, expected in originals.items():
        if path.read_bytes() != expected:
            raise ContractError("專案在準備期間又被修改，尚未套用；請先保存並關閉 A/B")
    wrote_source = False
    try:
        atomic_json(source_path, source)
        wrote_source = True
        if review_path.read_bytes() != originals[review_path]:
            raise ContractError("校對專案在改號時被其他視窗更新，已停止並回復來源")
        atomic_json(review_path, corrected)
    except Exception:
        if wrote_source:
            # 只回復本工具剛寫入的版本，不覆蓋其他程式隨後保存的資料。
            if json.loads(source_path.read_text(encoding="utf-8")) == source:
                atomic_bytes(source_path, originals[source_path])
        raise
    return plan
