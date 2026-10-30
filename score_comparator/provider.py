"""比較工具獨立 API 邊界：無自動重試，失敗與格式錯誤仍保存已收到的用量。"""
import json
import time
from score_recognizer.workspace import now

from score_recognizer.contract import ContractError, validate_result, check_shape, RESPONSE_SCHEMA

# 2026-10-28 官方一般輸入／輸出單價（USD / 百萬 tokens）。
# 粗估不扣快取，也不計區域／服務層級加價，不能當作帳單。
RATES = {"gpt-6-luna": (.10, .50), "gpt-6.1-terra": (1., 5.),
         "gpt-6-sol": (2., 10.), "gpt-6.1-sol": (2., 10.), "gpt-6-astra": (10., 50.)}


def estimated_cost(model, usage):
    if model not in RATES or not isinstance(usage, dict):
        return None
    counts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    if any(type(v) is not int or v < 0 for v in counts):
        return None
    return sum(n * rate for n, rate in zip(counts, RATES[model])) / 1_000_000


def execute(request, region, *, api_key=None, client=None):
    start = time.monotonic()
    record = {"status": "failed", "result": None, "raw_output": None, "usage": None,
              "response_id": None, "error": None, "cost_usd": None, "elapsed_seconds": None}
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, timeout=600, max_retries=0)
        response = client.responses.create(**request)
        usage = getattr(response, "usage", None)
        record["usage"] = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else usage
        record["response_id"] = getattr(response, "id", None)
        record["served_model"] = getattr(response, "model", request["model"])
        record["raw_output"] = getattr(response, "output_text", None)
        record["cost_usd"] = estimated_cost(request["model"], record["usage"])
        if getattr(response, "status", "completed") != "completed":
            record["error"] = "回應未完成或被截斷；已保存回應及可取得的用量"
        elif not record["raw_output"]:
            record["error"] = "未收到可用文字結果，可能被拒絕"
        else:
            try:
                parsed = json.loads(record["raw_output"])
                check_shape(parsed, RESPONSE_SCHEMA)
                if len(parsed["measures"]) != 1 or parsed["measures"][0]["index"] != 0:
                    raise ContractError("未依請求回報唯一的單小節，請查看原始回應")
                # 形狀可讀的候選保留給差異檢視；時間超界不把整份答案隱藏。
                record["result"] = parsed
                validate_result(parsed, region)
                record["status"] = "succeeded" if parsed["measures"][0]["parts"] else "empty"
                if record["status"] == "empty":
                    record["error"] = "未回報任何聲部；不能當作整小節休止，需對照原圖"
            except (ValueError, TypeError, KeyError, ContractError) as error:
                record.update(status="invalid", error=f"回應格式或小節結構需檢查：{error}")
    except Exception as error:
        code = getattr(error, "status_code", None)
        reason = {401: "金鑰無效或沒有權限", 403: "模型權限不足", 429: "額度或速率限制", 400: "模型或設定不支援"}.get(code, "連線、逾時或服務異常")
        record["error"] = f"{reason}（{type(error).__name__}）；未自動重試，沒有用量時不代表沒有計費"
    record["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return record


def run_cases(project, cases, *, api_key, stop, notify=lambda *args: None, executor=execute):
    """一個模型完成就寫檔；停止僅阻止後續請求，不丟棄在途回應。"""
    for case in cases:
        for run in case["runs"]:
            if run["status"] != "queued":
                continue
            if stop.is_set():
                run.update(status="cancelled", error="尚未送出的比較已停止")
                project.save()
                continue
            run.update(status="running", started_at=now())
            project.save()
            notify("started", case["case_id"], run["model"])
            request = project.request_for(case, run["model"])
            run.update(executor(request, case["source_snapshot"], api_key=api_key))
            project.save()
            notify("result", case["case_id"], run["model"])
