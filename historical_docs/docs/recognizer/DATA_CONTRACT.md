# 程式 A → B 辨識交換格式 1.0

以本次討論的決議為準；`docs/restart/` 保留為舊方案參考。A 不要求先建立 Segment。

## 工作資料與匯出

工作資料夾副檔名 `.score-recognition`，主檔 `recognition-package.json`；原圖位於 `assets/original/<sha256>.<jpg|png>`。
主檔暫存寫入後原子替換，備份為 `recognition-package.json.bak`。
匯出 ZIP 保存相同主檔與原圖，另加 `export_summary`、`active_run_ids`、`exported_at`。
搬移時使用整個資料夾，或解開 ZIP；不能只搬主檔。來源相對路徑與照片 SHA-256 送出前及匯出時驗證。

主欄位：

- `format = musicai-recognition-package`、`schema_version = 1.0`、`ppq = 960`。
- `package_id`、`revision`、建立／修改時間、樂曲標題。
- `assets`：照片、檔名、紙面頁碼、頁序、來源雜湊。
- `regions`：主框／參考框、紙面起始小節、小節數、分界、譜種、拍號、調號、備註及使用者確認狀態。
- `recognition_runs`：不可因後續修改而丟棄的每次辨識歷史。
- `instrument_library_snapshot`：沿用已有穩定 ID，但不把新辨識名稱自動加入主庫。
- `score_context`：使用者提供的共通背景資訊。
- `reference_annotations`（選用）：人工確認的校對參考；不是 AI 結果。

## 來源與時間

框座標皆為 EXIF 校正後整張圖片的 0..1；AI 原始 bbox 則在主裁圖內，正規化 observations 已換回原圖。
`boundaries` 是主裁圖內的橫向 0..1 分界，小節數 N 需要 N+1 條；第一條左側可容納樂器名稱。
紙面小節 `start_bar + measure.index` 不隨反覆重新編號。

`onset` 和 `duration` 用四分音符為單位的精確分數 `{n,d}`；onset 從小節內 0 開始。
能精確轉成 960 PPQ 整數的數值另存 tick 欄位，其他值仍保留分數，不擅自四捨五入。
不明資訊使用 null 與 `uncertainties`；不是零、不是休止、不是自動的一般敲擊。

## 原始觀察

每次原始回應有 `notation`、`measures`、`navigation`、`warnings`，嚴格 schema 定義於 `score_recognizer/contract.py`。
每個 measure 回報所有可見聲部；part 分為 events、full_rest、unclear。
event 分為 note、rest、unreadable。full_rest 保存整小節存在的證據，events 為空。

每個事件分開保存：

- 原符號、來源聲部與當下指定樂器原文；未知名稱照存。
- onset、duration、同時發聲的 pitches。
- 簡譜 degree／octave_shift，五線譜 step／octave／clef，升降記號、MIDI（未知可空）。
- is_roll、is_accent、is_ornament、tie、slur、dynamic。
- bbox 與欄位不確定原因。

輪奏是單一有時值的事件；播放快速敲擊由 B 處理。延音與連奏分開。
人工核對前不得把簡譜數字直接當擊法編號，也不得在沒有八度證據時猜 MIDI。

## 正規化 observations

A 每次 run 產生 `observations`；含穩定的 `run_id:序號` 觀察 ID、紙面小節、原圖 bbox、instrument_id（無法對應為 null）及 review_status=pending。
`technique_id` 等待 B 指定，AI 原文不被覆蓋。未知音高、技巧、來源疑問各自保存。
同一聲部在部分小節未回報，會增加 unreadable 觀察；不當成休止。

## 反覆

`navigation` 使用整首的紙面小節號，kind 為 repeat_start、repeat_end、ending、other。
ending 用起訖小節與 passes 表示第一／第二結尾，允許跨頁引用。
各聲部重印的記號應只回報一次，B 仍需做整首配對與展開檢查。
校對例：起點 1、終點 8、第一結尾 8、第二結尾 9，演奏順序 1–8 → 1–7 → 9 起。
音符只按紙面保存一次。

## 歷史與 B 的界線

每次 run 保存來源 snapshot、fingerprint、模型、原始輸出、用量、錯誤與完成狀態。
來源改變後 `latest_run` 不會返回過期結果；匯出的 `active_run_ids` 指出目前來源相符且成功／空結果的最新 run。
同來源若最新重試失敗，前一次成功僅留歷史，不自動當成新結果。`raw_output` 在格式驗證失敗時也保留。
明確拆分取代已有結果的範圍時，原範圍從 active regions 移出，保存於可選欄位 `retired_regions`（region 快照、時間、原因、replacement_region_ids）；recognition_runs 完整保留，新子範圍使用新 region_id，初始為未確認且不承接舊答案。running 範圍不可拆分。B 匯入時將已移除 region_id 的現有 entries 移到 archived_entries，保留人工值與狀態，並使受影響的反覆範圍待重新確認；重複匯入不重複封存或重置已重新確認的反覆。
B 應另存自己的 review 文件，引用 package_id、revision、run_id、observation_id，不回寫 A 的原始觀察。
full_rest／rest 不建立敲擊；unreadable、未知名稱／技法需讓使用者補填，不可丟掉。
所有原始辨識仍屬候選，B 核對後才可形成正式播放或訓練資料。

## 官方 API 依據

- https://developers.openai.com/api/docs/guides/images-vision
- https://developers.openai.com/api/docs/guides/structured-outputs

透過 Responses API 的影像輸入與 strict JSON Schema；schema 正確並不保證音樂內容正確。
