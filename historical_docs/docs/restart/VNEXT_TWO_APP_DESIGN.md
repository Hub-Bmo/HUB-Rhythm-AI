# vNext：圖片辨識與資料確認的兩程式設計

本文件是下一版建議方案，尚未全部成為既定格式。凡標示「待決」者，必須在 Phase 1 由使用者確認後再實作。

## 1. 設計目標

- 使用者在任何時候都知道自己正在改「來源與辨識」還是「正式節奏資料」。
- OpenAI 的輸出永遠先成為候選，不直接改格狀資料。
- 每個跨頁 Segment 的來源順序與小節對應可直接看見、可驗證。
- AI 部分失敗、未知樂器、未知擊法與略過項目都有清楚報告。
- 兩個程式可分開關閉與重啟，照片、候選、人工修正與正式資料仍保持關聯。
- 既有 `.guoyue-project` 能被匯入，不要求使用者重做所有資料。

## 2. 建議工作資料夾

建議不要讓兩個程式共同任意修改同一個 `project.json`。使用單一可攜資料夾，但分成兩份 manifest：

```text
作品.guoyue-workspace/
├─ recognition-package.json   # 程式 A 擁有
├─ review-project.json        # 程式 B 擁有
├─ assets/
│  └─ original/
│     └─ <sha256>.<jpg|png>
└─ backups/
```

### 程式 A 可寫

- `assets`
- 頁序與全頁版面分析
- `measure_sequence`
- Segment 骨架與 `source_refs`
- `instrument_library_snapshot`
- `recognition_runs` 與原始 candidates
- 每次辨識的 request、模型、用量、錯誤與重試

### 程式 B 可寫

- 候選的人工 review 決定或獨立 review overlay
- 正式 Segment metadata
- Track / Take / Event
- navigation
- 播放／完成度／匯出狀態

### 不允許的隱性寫入

- 程式 A 不得直接建立正式 Event。
- 程式 B 不得在背景改動來源照片 crop 後繼續沿用舊 AI 候選。
- 程式 B 若發現來源框錯誤，建立「需回程式 A 修正」狀態；A 產生新 package revision，舊候選保留稽核但不可套用。

## 3. Recognition Package（建議 v1）

```json
{
  "format": "guoyue-recognition-package",
  "schema_version": "1.0",
  "package_id": "PKG-...",
  "revision": 1,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "ppq": 960,
  "assets": [],
  "score_plan": {},
  "segments": [
    {
      "segment_id": "BG0001",
      "piece_id": "P0001",
      "piece_name": "",
      "section_name": "",
      "bars": 13,
      "time_signature": "4/4",
      "tempo": 90,
      "subdivision": "1/16",
      "bar_settings": [],
      "source_refs": []
    }
  ],
  "instrument_library_snapshot": {},
  "recognition_runs": [],
  "package_summary": {
    "segments": 0,
    "source_regions": 0,
    "runs_succeeded": 0,
    "runs_failed": 0,
    "pending_candidates": 0,
    "unknown_instruments": 0,
    "unknown_strokes": 0
  }
}
```

要求：

- `package_id + revision` 唯一識別一次可審核輸入。
- `recognition-package.json` 完整保存辨識原始資料，不因 B 的人工修正而被覆蓋。
- 每個 Segment 的 `source_refs` 必須通過跨頁連續性驗證後才可輸出。
- package 允許部分成功；失敗的 run 仍保留錯誤，不阻止其他 run 進入 B。
- 匯出前顯示摘要，使用者一眼可知是否少頁、少 Segment 或少樂器。

## 4. Review Project（建議 v1）

```json
{
  "format": "guoyue-review-project",
  "schema_version": "1.0",
  "project_id": "PRJ-...",
  "source_package": {
    "package_id": "PKG-...",
    "revision": 1,
    "sha256": "..."
  },
  "ppq": 960,
  "candidate_reviews": {},
  "segments": [],
  "review_summary": {},
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

`candidate_reviews` 建議用穩定的 `run_id:candidate_id` 當 key：

```json
{
  "RUN-1:C-1": {
    "decision": "corrected",
    "corrected_data": {},
    "note": "",
    "reviewed_at": "ISO-8601"
  }
}
```

這樣可保留 A 的原始候選不變，又能讓 B 獨立保存進度。

正式 Track / Take / Event 可沿用現行 schema 2.1 的已確認欄位；若改名或升版，必須提供遷移測試。

## 5. 程式 A：圖片辨識流程

建議只保留五個主步驟：

1. **匯入照片**
   - 顯示縮圖、檔名、內容雜湊與頁序。
   - 使用者確認／排除頁面。

2. **確認小節**
   - 自動偵測只是建議。
   - 每條可拖曳，並有加入／刪除小節線的明確工具。
   - 顏色只表達一種意義，畫面上始終有圖例。

3. **規劃 Segment**
   - 用表格列出 Segment 及全域小節範圍。
   - 跨頁順序由 `page_sequence + measure_sequence` 決定。
   - 不用自由畫框來暗中更改 Segment。

4. **確認來源對應**
   - 每一列顯示：Segment、來源頁、`region_id`、全域小節、Segment 局部小節、主 bbox、列名 context bbox、狀態。
   - 若局部小節不連續或重疊，禁止送 AI。

5. **辨識與匯出**
   - 送出前預覽實際裁圖。
   - 一個 `source_ref` 一個獨立 run；批次只是排程容器。
   - 顯示 `排隊／處理中／成功／空結果／失敗／需重試`。
   - 匯出 Recognition Package，不建立正式 Event。

## 6. 程式 B：資料確認流程

建議只保留六個主步驟：

1. **載入套件並檢查完整性**
   - 驗證 package hash、照片存在、960 PPQ、Segment 來源連續性。

2. **總覽**
   - 以 Segment × 樂器矩陣顯示候選數。
   - 明確標出 `unknown instrument`、`unknown stroke`、失敗 run 與空結果。

3. **逐項審核**
   - 左側原圖與證據框，右側候選欄位。
   - 接受、修正、拒絕均可復原。
   - 休止 `0` 顯示為時間證據或文字證據，不建立 Event。

4. **反覆與不同結尾**
   - 顯示紙面小節與展開後順序。
   - AI 反覆候選只是提示，必須人工確認。
   - 儲存前檢查配對、範圍、passes 與跨 Segment 問題。

5. **產生正式資料**
   - 先顯示套用預覽：將建立哪些 Track、每軌多少 Event、哪些項目略過及原因。
   - 使用者確認後才建立 AI Take。
   - 重複套用同一 candidate 必須是 idempotent。

6. **格狀檢查與播放**
   - 只讀正式選取 Take。
   - 原圖、格子與聲音播放可同步定位。
   - 人工修正寫入新的修訂紀錄，仍保留 AI 來源。

## 7. 反覆記號的待決事項

目前已確認：

- 反覆不複製 Event。
- 可表示一般反覆與第一／第二結尾。
- 展開結果供播放與 token 化使用。

Phase 1 必須決定：

1. 反覆是否保證只在單一 Segment 內？
2. 若反覆跨 Segment，是：
   - 合併 Segment；或
   - 新增 piece-level navigation，讓展開項目引用 `{segment_id, source_bar}`？
3. AI 發現的反覆記號是只掛在來源頁，還是立刻映射到 Segment 局部小節？
4. D.C.、D.S.、Coda、Segno 等跳轉是否納入 vNext 第一版？目前底層尚未正式支援，建議第一版不做。

建議：第一版只支援**單一 Segment 內**的一般反覆與第一／第二結尾；遇到跨 Segment 記號時明確標示「不支援，請調整 Segment 或待後續 piece-level navigation」，不要猜。

## 8. 來源框與 Segment 的新規則

為消除現行混亂，vNext 應遵守：

- 畫面上的自由框選只是草稿，未按「確認來源對應」前不改資料。
- 確認時必須同時指定：`segment_id`、`asset_id`、`segment_start_bar`、`segment_end_bar`。
- 每次送 AI 都只讀已確認的 `source_ref`，不讀臨時畫面框。
- 修改來源框會產生新的 package revision；舊 run 變成歷史資料。
- 已在 B 生成的 Event 不自動刪除；B 必須顯示來源 package 已過期，要求使用者選擇保留、重做或比較。
- 所有線框均有固定圖例，例如：
  - 藍色實線：已確認來源框；
  - 橘色實線：小節邊界；
  - 綠色虛線：AI 候選證據；
  - 灰色虛線：尚未確認草稿。

顏色只是建議；關鍵是不允許同一顏色在不同頁面代表不同概念。

## 9. 必須通過的端到端驗收

準備一份固定測試樂譜，至少包含：

- 3 頁；
- 13 小節且跨頁；
- 2 個以上 Segment；
- 至少 3 種樂器，包含定音鼓與非定音鼓；
- 數字譜中的 `0`；
- 重音、裝飾音、滾奏；
- 一組一般反覆，最好再含第一／第二結尾；
- 一個故意無法對應的樂器或擊法；
- 一個故意失敗的辨識 run。

驗收條件：

1. A 能保存並重開完整頁序、Segment 來源與辨識狀態。
2. 13 小節的跨頁 `source_refs` 局部編號為 1–13，無重疊、無缺口。
3. 一個 run 失敗不影響其他 run 的候選進入套件。
4. B 可逐項接受／修正／拒絕，重開後進度不遺失。
5. 套用預覽按 Segment × Track 顯示結果；非定音鼓資料不可靜默消失。
6. 所有略過項目都有具體原因與修正入口。
7. `0` 不建立 Event，但後續事件時間正確。
8. 重音、裝飾音、滾奏與 duration 可在格狀資料中看到並播放檢查。
9. 反覆展開只改播放順序，不增加原始 Event 數。
10. 第二次套用相同候選不會產生重複 Event。
11. 修改 A 的來源框後，B 明確提示 package revision 改變，不暗中沿用舊結果。
12. 匯出前能列出每個 Segment、Track、selected Take 與未完成項目。

## 10. 舊專案遷移原則

- 舊 `project.json` 原檔只讀，不原地覆寫。
- 搬移 `assets`、`score_plan`、Segment 骨架、`source_refs`、樂器快照與 `recognition_runs` 到 Recognition Package。
- 搬移人工／點擊／已存在的正式 Track、Take、Event 與 navigation 到 Review Project。
- 以 `source_run_id` / `source_candidate_id` 建立 AI Event 的來源關聯。
- 找不到來源的既有 Event 標記為 `legacy_unlinked`，保留而不刪除。
- 缺少 `segment_start_bar` / `segment_end_bar` 的跨頁來源必須人工確認，不可自動猜測後直接通過。
- 遷移完成後產生報告：已搬移、需人工處理、無法對應、未使用的照片與失敗 runs。

## 11. 控制 token 與範圍的方法

- 一次只開發一個 Phase。
- 每個 Phase 開始先寫「不可改的資料契約」與驗收測試，再改程式。
- 不在同一回合重做 UI、schema、AI prompt、播放四個層次。
- 新對話只讀三份重啟文件及本 Phase 直接相關的模組。
- 先使用固定的假辨識 JSON 完成 A→B 流程；最後才接 OpenAI。
- 每完成一個垂直小流程就讓使用者試用，不累積十個新按鈕後才驗收。
- 一旦需求變更影響 schema，先更新本文件並確認，再動實作。

