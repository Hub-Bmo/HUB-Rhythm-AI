"""本機 Audiveris；只產生候選，從不在此模組呼叫付費 API。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import time

from guoyue_annotator.openai_settings import default_settings_path
from guoyue_annotator.photo_assets import asset_file_path
from .contract import ContractError
from .musicxml import import_musicxml, read_score
from .omr_image import enhance, strengthen_connection
from .provider import crop_image
from .ocr_resources import prepare_tessdata, verify_legacy_models


def settings_path():
    return default_settings_path().with_name("omr-settings.json")


def find_audiveris(configured=None):
    candidates = []
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.suffix.lower() == ".app":
            candidate /= "Contents/MacOS/Audiveris"
        return candidate.resolve() if candidate.is_file() else None
    elif os.environ.get("MUSICAI_AUDIVERIS"):
        candidates.append(Path(os.environ["MUSICAI_AUDIVERIS"]).expanduser())
    elif settings_path().is_file():
        try:
            value = json.loads(settings_path().read_text(encoding="utf-8")).get("audiveris_path")
            if value:
                candidates.append(Path(value).expanduser())
        except (ValueError,OSError):
            pass
    root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        candidates += [root/".tools/audiveris/runtime/Audiveris/Audiveris.exe",
                       Path(os.environ.get("ProgramFiles",r"C:\Program Files"))/"Audiveris/Audiveris.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Audiveris.app/Contents/MacOS/Audiveris"),
                       Path.home()/"Applications/Audiveris.app/Contents/MacOS/Audiveris"]
    for name in ("audiveris","Audiveris"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.suffix.lower() == ".app":
            candidate /= "Contents/MacOS/Audiveris"
        if candidate.is_file():
            return candidate.resolve()
    return None


def save_audiveris_path(path):
    executable = find_audiveris(path)
    if not executable or not Path(path).exists():
        raise ContractError("找不到 Audiveris 執行檔，請選擇已安裝的 Audiveris 程式")
    target = settings_path()
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps({"audiveris_path":str(executable)},ensure_ascii=False,indent=2),encoding="utf-8")
    return executable


def _execute(command, directory, env, log_path, region, stop, timeout, status):
    with log_path.open("w",encoding="utf-8") as log:
        process = subprocess.Popen(command,cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        started = time.monotonic()
        last_update = 0
        phase = "準備辨識"
        phases = {"LOAD":"讀取圖片","BINARY":"整理影像","SCALE":"估算譜線","GRID":"定位譜表與小節線",
                  "HEADERS":"讀取譜號與拍號","HEADS":"辨認符頭","STEMS":"辨認符桿","TEXTS":"讀取樂器與演奏文字",
                  "MEASURES":"整理小節","CHORDS":"整理音符","CURVES":"辨認連線","RHYTHMS":"重建節奏","PAGE":"整合並輸出"}
        try:
            while process.poll() is None:
                if stop is not None and stop.is_set():
                    raise ContractError("本機 OMR 已停止；原圖與處理紀錄已保留，可免費重試。")
                if time.monotonic()-started > timeout:
                    raise ContractError("本機 OMR 超過 10 分鐘，已停止；請檢查斜框、影像品質或拆小範圍。")
                if status and time.monotonic()-last_update >= 2:
                    try:
                        with log_path.open("rb") as progress_log:
                            progress_log.seek(0,2)
                            progress_log.seek(max(0,progress_log.tell()-16000))
                            found = re.findall(r"StepMonitoring\s+\d+\s+\|\s+(\w+)",progress_log.read().decode("utf-8",errors="replace"))
                        if found:
                            phase = phases.get(found[-1],found[-1])
                    except OSError:
                        pass
                    status(f"本機 OMR 小節 {region['start_bar']}–{region['start_bar']+region['bar_count']-1}：{phase}（{time.monotonic()-started:.0f} 秒）")
                    last_update = time.monotonic()
                time.sleep(.2)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return process.returncode


def _matches(paths, region):
    if len(paths)!=1:
        return False
    try:
        parts=read_score(paths[0]).findall("part")
        return bool(parts) and all(len(p.findall("measure"))==region["bar_count"] for p in parts)
    except (ContractError, ValueError, OSError):
        return False


def recognize_local(workspace, region, run, *, executable=None, stop=None, timeout=600, status=None):
    if region["notation"] not in {"staff","auto"}:
        raise ContractError("本機 OMR 只處理印刷五線譜；簡譜、鑼鼓經及手寫譜請改選 OpenAI，或先提供圖例。")
    executable = find_audiveris(executable)
    if executable is None:
        raise ContractError("尚未安裝／指定 Audiveris。請按「本機 OMR 設定」，PC 請選 Windows x86_64 版；不會自動送到 OpenAI。")
    directory = workspace.root/"omr"/run["run_id"]
    directory.mkdir(parents=True,exist_ok=False)
    run["engine"] = "audiveris"
    run["artifacts"] = {"directory":directory.relative_to(workspace.root).as_posix()}
    image_path = directory/"primary.png"
    source = asset_file_path(workspace.root, workspace.asset(region["asset_id"]),must_exist=True)
    original=crop_image(source,region["bbox"],region.get("quad"))
    original.save(image_path)
    processed=enhance(original)
    processed_path=directory/"omr-input.png"
    processed.save(processed_path)
    run["artifacts"]["primary_image"]=image_path.relative_to(workspace.root).as_posix()
    run["artifacts"]["processed_images"]=[processed_path.relative_to(workspace.root).as_posix()]
    run["omr_preprocessing"]=[{"kind":"grayscale_autocontrast","cutoff":.5}]
    command = [str(executable),"-batch","-transcribe","-export","-save","-output",str(directory),"--",str(processed_path)]
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY",None)
    tessdata_candidates = [Path(__file__).resolve().parents[1]/".tools/audiveris/tessdata",settings_path().parent/"omr/tessdata"]
    if not env.get("TESSDATA_PREFIX"):
        for folder in tessdata_candidates:
            if (folder/"eng.traineddata").is_file():
                env["TESSDATA_PREFIX"] = str(folder)
                break
    tessdata = prepare_tessdata(env["TESSDATA_PREFIX"]) if env.get("TESSDATA_PREFIX") else None
    if tessdata:
        env["TESSDATA_PREFIX"] = str(tessdata)
    if tessdata and (tessdata/"eng.traineddata").is_file() and (tessdata/"chi_tra.traineddata").is_file():
        verify_legacy_models(tessdata,("eng","chi_tra"))
        command[1:1] = ["-constant","org.audiveris.omr.text.Language.defaultSpecification=eng+chi_tra"]
    # 每份專案有獨立的 OMR 暫存／設定；不與其他 Audiveris 視窗搶寫。
    profile = workspace.root/"omr"/"runtime-profile"
    profile.mkdir(parents=True,exist_ok=True)
    if sys.platform == "win32":
        env["APPDATA"] = str(profile)
    elif sys.platform != "darwin":
        env["XDG_CONFIG_HOME"] = str(profile/"config")
        env["XDG_DATA_HOME"] = str(profile/"data")
        env["XDG_CACHE_HOME"] = str(profile/"cache")
    log_path = directory/"audiveris.log"
    run["artifacts"]["log"] = log_path.relative_to(workspace.root).as_posix()
    workspace.save()
    started=time.monotonic()
    returncode=_execute(command,directory,env,log_path,region,stop,timeout,status)
    candidates = sorted(directory.rglob("*.mxl")) or sorted(directory.rglob("*.musicxml"))
    run["artifacts"]["omr_files"] = [p.relative_to(workspace.root).as_posix() for p in directory.rglob("*.omr")]
    log_text=log_path.read_text(encoding="utf-8",errors="replace")
    if any(message in log_text for message in ("No installed OCR languages", "Missing support for 'eng+chi_tra'", "Could not initialize TessBaseAPI", "Tesseract couldn't load any languages")):
        raise ContractError("Audiveris 沒有成功載入中英文文字辨識資料，這是本機環境問題。請重開已更新的程式 A 後再試；處理紀錄已保留，未呼叫 OpenAI。")
    if returncode != 0 or not candidates:
        raise ContractError("Audiveris 未成功產生 MusicXML。請在結果中查看本機紀錄；不會自動呼叫 OpenAI。")
    all_candidates=list(candidates)
    books=list(directory.glob("*.omr"))
    # 範圍代表同一排聲部；只加深原圖中已存在、橫跨各聲部間隙的淡直線。
    # 找不到原有連接線就保留失敗，不憑空合併聲部或編造音符。
    alternative=strengthen_connection(processed,books[0]) if not _matches(candidates,region) and books else None
    if alternative:
        retry_image,audit=alternative
        retry_directory=directory/"connected"
        retry_directory.mkdir()
        retry_path=retry_directory/"omr-input.png"
        retry_image.save(retry_path)
        run["omr_preprocessing"].append(audit)
        run["artifacts"]["processed_images"].append(retry_path.relative_to(workspace.root).as_posix())
        retry_command=list(command)
        retry_command[retry_command.index("-output")+1]=str(retry_directory)
        retry_command[-1]=str(retry_path)
        retry_log=retry_directory/"audiveris.log"
        run["artifacts"]["log_files"]=[log_path.relative_to(workspace.root).as_posix(),retry_log.relative_to(workspace.root).as_posix()]
        run["artifacts"]["log"]=retry_log.relative_to(workspace.root).as_posix()
        workspace.save()
        if status:
            status("加深原有聲部連接線後重新檢查；只在本機處理")
        remaining=timeout-(time.monotonic()-started)
        if remaining<=0:
            raise ContractError("本機 OMR 已達時間上限，原始處理紀錄保留")
        returncode=_execute(retry_command,retry_directory,env,retry_log,region,stop,remaining,
                            (lambda message:status("重試："+message)) if status else None)
        candidates=sorted(retry_directory.rglob("*.mxl")) or sorted(retry_directory.rglob("*.musicxml"))
        all_candidates+=candidates
        run["artifacts"]["omr_files"]=[p.relative_to(workspace.root).as_posix() for p in directory.rglob("*.omr")]
        if returncode!=0 or not candidates:
            raise ContractError("加深聲部連接線後仍未產生可用 MusicXML，請查看本機處理紀錄")
    run["artifacts"]["musicxml_files"]=[p.relative_to(workspace.root).as_posix() for p in all_candidates]
    if len(candidates) != 1:
        workspace.save()
        raise ContractError(f"OMR 把這個範圍分成 {len(candidates)} 份樂譜，可能未能連接各聲部或小節線。已保留輸出；請調整斜框／對比清晰度，或改選整範圍 OpenAI。不能只取第一份而漏掉其他聲部。")
    xml_path = candidates[0]
    run["artifacts"]["musicxml"] = xml_path.relative_to(workspace.root).as_posix()
    workspace.save()
    result, review = import_musicxml(xml_path,region)
    if alternative:
        result["warnings"].append("OMR 已加深原有聲部連接線，請核對同排聲部分組；原圖與兩次處理紀錄均保留。")
    run["omr_review"] = review
    run["omr_unchecked_bars"] = list(range(region["bar_count"]))
    return {"result":result,"raw_output":json.dumps(result,ensure_ascii=False),"usage":None,"response_id":None}


def supplement_with_openai(workspace, region, base_run, run, *, model, api_key, detail, stop=None, status=None, recognizer=None):
    """只重讀本機結構檢查提出疑問的小節，逐筆留存回應後合併。"""
    from .provider import recognize
    from .geometry import region_point, quad_bounds
    recognizer = recognizer or recognize
    base_result = base_run.get("partial_result") or base_run.get("result")
    if not base_result or "omr_review" not in base_run:
        raise ContractError("目前沒有可補辨的 OMR 結果；可先執行本機 OMR，或明確選擇整範圍 OpenAI 辨識")
    targets = sorted({item["index"] for item in base_run["omr_review"]})
    if not targets:
        raise ContractError("本機未提出結構疑問；這不代表音樂內容一定正確，可先人工抽查或選擇整範圍 OpenAI 辨識")
    run.update(engine="omr+openai",omr_base_run_id=base_run.get("omr_base_run_id") or base_run["run_id"],supplemental_base_run_id=base_run["run_id"],artifacts=deepcopy(base_run.get("artifacts",{})),
               supplemental_responses=[],omr_review=deepcopy(base_run["omr_review"]))
    merged = deepcopy(base_result)
    unchecked = set(base_run.get("omr_unchecked_bars",range(region["bar_count"])))
    run["partial_result"] = deepcopy(merged)
    # 原 OMR 導覽先保留；補辨回報的同範圍候選另行去重，絕不自動展開反覆。
    usage = {"input_tokens":0,"output_tokens":0,"total_tokens":0}
    for order,index in enumerate(targets):
        if stop is not None and stop.is_set():
            raise ContractError("已停止後續 OpenAI 補辨；先前收到的補辨回應已保存在這次紀錄中，原 OMR 結果仍保留。")
        sub = deepcopy(region)
        left,right = region["boundaries"][index:index+2]
        crop_left = 0 if index == 0 else left
        sub["quad"] = [region_point(region,u,v) for u,v in ((crop_left,0),(right,0),(right,1),(crop_left,1))]
        sub["bbox"] = quad_bounds(sub["quad"])
        sub.update(start_bar=region["start_bar"]+index,bar_count=1,boundaries=[(left-crop_left)/(right-crop_left),1],
                   context_bbox=region.get("context_bbox") or region["bbox"],
                   context_notes=region["context_notes"]+"\n只辨識這一小節；第二張圖提供原範圍的名稱、拍號、變拍與反覆背景，不抄入其他小節。")
        if status:
            status(f"OpenAI 補辨小節 {sub['start_bar']}（{order+1}/{len(targets)}）")
        response = recognizer(workspace,sub,model=model,api_key=api_key,detail=detail)
        from .contract import validate_result
        run["supplemental_responses"].append({"bar":sub["start_bar"],"source_snapshot":sub,**deepcopy(response)})
        # 付費回應一收到就保存；後面的請求失敗也不遺失已收到的原始內容。
        run["usage"] = deepcopy(usage)
        for key in usage:
            usage[key] += (response.get("usage") or {}).get(key,0) or 0
        run["usage"] = deepcopy(usage)
        workspace.save()
        validate_result(response["result"],sub)
        measure = deepcopy(response["result"]["measures"][0])
        measure["index"] = index
        # 補辨框的局部座標回到本次整範圍攤正座標。
        def rebase(box):
            return {"x":crop_left+box["x"]*(right-crop_left),"y":box["y"],
                    "width":box["width"]*(right-crop_left),"height":box["height"]}
        for part in measure["parts"]:
            part["bbox"] = rebase(part["bbox"])
            for event in part["events"]:
                event["bbox"] = rebase(event["bbox"])
        merged["measures"][index] = measure
        merged["navigation"] = [m for m in merged["navigation"] if m["start_bar"] != sub["start_bar"] or (m.get("end_bar") or m["start_bar"]) > sub["start_bar"]]
        for marker in response["result"]["navigation"]:
            marker = deepcopy(marker)
            marker["bbox"] = rebase(marker["bbox"])
            merged["navigation"].append(marker)
        merged["warnings"].extend(response["result"]["warnings"])
        run["omr_review"] = [item for item in run["omr_review"] if item["index"] != index]
        unchecked.discard(index)
        run["omr_unchecked_bars"] = sorted(unchecked)
        run["partial_result"] = deepcopy(merged)
        workspace.save()
    merged["warnings"].append("僅 OMR 結構疑問小節經 OpenAI 補辨；其餘小節尚未經 OpenAI 檢查，全部仍待人工核對。")
    return {"result":merged,"raw_output":json.dumps(merged,ensure_ascii=False),"usage":usage,"response_id":None}
