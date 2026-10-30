# 現行資料契約（已確認的事實）

本文件只記錄目前程式已實作、應被保留或明確遷移的資料規則。它不是 vNext UI 規格。若本文件與舊對話描述衝突，以實際程式碼和後續明確決議為準。

## 1. 核心名詞與層級

```text
Project
├─ Assets：原始照片
├─ Score Plan：頁序、偵測小節、建議 Segment
├─ Recognition Runs：AI 辨識工作與候選
├─ Instrument Library Snapshot：本專案引用的樂器／擊法快照
└─ Segments
   ├─ Source Refs：本段落對應的照片區域與小節
   ├─ Navigation：反覆與不同結尾
   └─ Tracks：每個樂器一軌
      └─ Takes：人工、點擊或 AI 等不同版本
         └─ Events：正式敲擊事件
```

資料原則：

- `Recognition Candidate` 是待確認資料，不等於正式 Event。
- `Event` 只能存在於某個 Track 的某個 Take。
- AI 資料建立獨立 Take，不覆蓋人工 Take。
- Track 仍分樂器保存；只有播放或 token 化時才沿共同時間軸組合。
- 正式時間基準固定為 `PPQ = 960`。

## 2. 可攜專案資料夾

現行專案以副檔名為 `.guoyue-project` 的資料夾保存：

```text
範例.guoyue-project/
├─ project.json
└─ assets/
   └─ original/
      └─ <sha256>.<jpg|png>
```

- `project.json` 是 manifest。
- 照片以相對路徑寫入專案，專案移動後仍可找到。
- 儲存採暫存檔替換，並保留可恢復備份。
- OpenAI API key 不寫入專案。

## 3. Project（schema 2.1）

主要欄位：

| 欄位 | 意義 |
|---|---|
| `schema_version` | 現行固定為 `"2.1"` |
| `ppq` | 現行只接受 `960` |
| `input_mode`、`input_group` | 舊輸入流程相容欄位 |
| `hit_sound_enabled` | 即時敲擊聲是否開啟 |
| `next_event_number` | 產生穩定 Event ID 的流水號 |
| `assets` | 照片資產 |
| `recognition_runs` | AI 辨識歷程 |
| `score_plan` | 整份樂譜頁序、小節與 Segment 建議 |
| `instrument_library_snapshot` | 樂器庫快照 |
| `workspace_state` | 目前照片、縮放、捲動等介面狀態；不可視為音樂資料 |
| `segments` | 正式段落集合 |

未知欄位在 migration 時應儘量保留，不應任意丟棄。

## 4. Asset：來源照片

```json
{
  "asset_id": "IMG-<sha256 前 24 碼>",
  "kind": "source_image",
  "relative_path": "assets/original/<sha256>.jpg",
  "sha256": "...",
  "byte_size": 123456,
  "width": 1477,
  "height": 1108,
  "mime_type": "image/jpeg",
  "original_filename": "JC_1.jpg",
  "imported_at": "ISO-8601",
  "page_sequence": 1,
  "page_sequence_source": "import",
  "printed_page_number": null
}
```

- 目前只接受實際可解碼的 JPEG／PNG。
- `asset_id` 由內容雜湊產生，同圖可去重。
- 圖片方向會依 EXIF 校正；後續座標必須對應校正後影像。
- `page_sequence_source` 已知值為 `import`、`manual`、`detected`。

## 5. 座標與 BBox

所有保存的照片框以整張、已校正方向的圖片為座標空間：

```json
{"x": 0.10, "y": 0.20, "width": 0.70, "height": 0.15}
```

- `x`、`y`、`width`、`height` 都正規化為 `0..1`。
- `width`、`height` 必須大於 0。
- `x + width <= 1`，`y + height <= 1`。
- AI 供應商可先以裁切圖座標回傳，程式必須換算回整張圖片座標後才保存。

## 6. Score Plan 與小節序列

`score_plan` 現行主要欄位：

```json
{
  "version": "1.0",
  "status": "not_started",
  "page_asset_ids": [],
  "page_order_confirmed": false,
  "excluded_asset_ids": [],
  "analyses_by_asset": {},
  "measure_sequence": [],
  "segment_proposals": [],
  "updated_at": null
}
```

確認頁序後，小節可攤平成全譜 `measure_sequence`。單一小節至少要能指出：

```json
{
  "asset_id": "IMG-...",
  "system_id": "SYS-...",
  "bbox": {"x": 0.3, "y": 0.1, "width": 0.2, "height": 0.8},
  "system_bbox": {"x": 0.05, "y": 0.1, "width": 0.9, "height": 0.8},
  "origin": "detected",
  "global_bar": 10,
  "page_sequence": 2,
  "page_measure": 1,
  "confidence": 0.8
}
```

`segment_proposals` 是建議，不是正式音樂資料。只有套用建議後，才建立／更新正式 Segment 與 `source_refs`。

## 7. Segment

Segment 代表使用者確認的段落，現行主要結構：

```json
{
  "segment_id": "BG0001",
  "piece_id": "P0001",
  "piece_name": "",
  "pattern_name": "",
  "section_name": "",
  "selection_type": "",
  "selection_reason": "",
  "source": "",
  "source_page": "",
  "source_start_bar": "",
  "source_end_bar": "",
  "system": "",
  "time_signature": "4/4",
  "tempo": 90,
  "bars": 4,
  "ppq": 960,
  "subdivision": "1/16",
  "version": "1.0",
  "notes": "",
  "bar_settings": [],
  "source_refs": [],
  "navigation": {
    "markers": [],
    "playback_order": [],
    "review_status": "not_started"
  },
  "review": {
    "status": "not_started",
    "by_track": {},
    "updated_at": null
  },
  "tracks": []
}
```

### 7.1 `bar_settings`

每個段落內小節各有一筆：

```json
{"bar": 1, "time_signature": "4/4", "subdivision": "1/16"}
```

- `bar` 是 **Segment 內從 1 開始** 的小節號。
- 拍號與 subdivision 可逐小節不同。
- 改變格線時，既有 Event 需要依規則重新對齊，不能只改畫面。

### 7.2 `source_refs`：照片與 Segment 的正式橋樑

由整份樂譜規劃產生的完整記錄：

```json
{
  "region_id": "REG-...",
  "asset_id": "IMG-...",
  "bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8},
  "context_bbox": {"x": 0.05, "y": 0.1, "width": 0.9, "height": 0.8},
  "sequence": 1,
  "source_start_bar": 25,
  "source_end_bar": 28,
  "segment_start_bar": 1,
  "segment_end_bar": 4,
  "system_id": "SYS-...",
  "origin": "detected",
  "review_status": "confirmed",
  "created_at": "ISO-8601"
}
```

欄位意義：

- `source_start_bar` / `source_end_bar`：整份紙本樂譜的全域小節號。
- `segment_start_bar` / `segment_end_bar`：這張裁切圖對應 Segment 內的局部小節號。
- `sequence`：同一 Segment 跨頁／跨譜表來源的前後順序。
- `bbox`：真正送去辨識的主要範圍。
- `context_bbox`：可選，用來提供左側樂器列名；不得從中抄入主要範圍外的節奏。
- `review_status`：現行允許 `pending`、`confirmed`、`rejected`。

不變條件：

- 同一 Segment 的局部小節範圍必須由 1 開始、連續、不重疊，最後涵蓋 `bars`。
- 每筆來源的全域小節數與局部小節數必須相同。
- 跨頁 Segment 依 `sequence` 還原順序，不可只依檔名猜測。

### 7.3 現行手動框選的風險

目前照片頁面的臨時 `selection_bbox` 只有在按下「連結框選到目前段落」後才成為 `source_ref`。之後 OpenAI 使用 `source_ref.bbox` 裁圖。

目前行為：

- 改寫既有框會將該 `region_id` 的舊辨識工作標為 `stale`。
- 已經從舊工作寫入正式 Take 的 Event 不會自動刪除。
- 手動新增來源框可能缺少完整的 `segment_start_bar` / `segment_end_bar` 對應。

因此 vNext 不應沿用「畫完框就隱含改 Segment」的操作；必須用可見的來源對應表確認。

## 8. 樂器庫與專案快照

全域樂器庫 schema 為 `1.0`：

```json
{
  "schema_version": "1.0",
  "library_id": "global",
  "revision": 1,
  "default_content_revision": 2,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "instruments": [
    {
      "instrument_id": "ins_<stable uuid>",
      "name": "定音鼓",
      "aliases": [],
      "sound_profile": "drum",
      "strokes": [
        {
          "stroke_id": "stk_<stable uuid>",
          "code": "pitch_6",
          "name": "音 6",
          "aliases": [],
          "color": "#..."
        }
      ]
    }
  ]
}
```

- `instrument_id`、`stroke_id` 建立後不可因改名而改變。
- 名稱、代碼與 aliases 用於人類顯示和辨識對應。
- AI 只能使用 request 快照中已有的 ID，否則必須回 `unknown`。
- 專案以 `instrument_library_snapshot` 保存當時使用的子集，避免全域樂器庫日後改名破壞舊資料。
- 未知項目不得自動污染全域樂器庫；應由使用者建立或對應後再使用。

## 9. Track

一個 Track 只代表一種樂器：

```json
{
  "track_id": "BG0001-T01",
  "segment_id": "BG0001",
  "instrument_id": "ins_...",
  "instrument": "定音鼓",
  "sound_profile": "drum",
  "track_order": 1,
  "is_complete": false,
  "selected_take_id": null,
  "notes": "",
  "keymap": {"f": "pitch_1"},
  "brushes": [
    {
      "code": "pitch_1",
      "label": "音 1",
      "color": "#...",
      "stroke_id": "stk_..."
    }
  ],
  "takes": []
}
```

- `instrument_id` 是正式對應；`instrument` 是顯示／舊版相容字串。
- 同一 Segment 若有同一 `instrument_id` 的多個 Track，AI 自動套用會產生歧義，必須人工解決。
- Track 名稱不同不代表不同樂器；穩定 ID 才是判定依據。

## 10. Take

人工格狀 Take 範例：

```json
{
  "take_id": "TAKE-...",
  "created_at": "ISO-8601",
  "quantization": "per-bar-grid",
  "count_in_bars": 0,
  "output_latency_ms": 0,
  "input_method": "grid",
  "review_status": "accepted",
  "events": []
}
```

AI 套用產生的 Take 使用：

- `input_method = "ai-recognition"`
- `review_status = "needs_review"`

Track 的 `selected_take_id` 決定播放與主要匯出使用哪個 Take。不同 Take 不可在未告知使用者時互相覆蓋。

## 11. Event：正式節奏單位

```json
{
  "event_id": "E000001",
  "segment_id": "BG0001",
  "track_id": "BG0001-T01",
  "take_id": "TAKE-...",
  "bar": 1,
  "beat": 1.0,
  "tick": 0,
  "raw_tick": 0,
  "onset_ms": null,
  "stroke": "pitch_6",
  "stroke_id": "stk_...",
  "is_accent": false,
  "is_ornament": false,
  "is_roll": false,
  "duration": null,
  "velocity": 100,
  "key_code": "1",
  "input_group": "grid",
  "quantization": "1/16",
  "grid_index": 0,
  "input_method": "grid"
}
```

時間欄位：

- `bar`：Segment 內從 1 開始的小節號。
- `tick`：Segment 內的絕對 tick；不是小節內 tick。
- AI 候選中的 `data.tick` 則是 **該小節內從 0 開始** 的 tick；套用時才換算成正式絕對 `tick`。
- `raw_tick`：量化前或原始對齊位置；人工格狀輸入可與 `tick` 相同。
- `beat`：該小節內從 1 開始的拍位置，供顯示與匯出。

演奏法：

- `is_accent`：重音。
- `is_ornament`：裝飾音。
- `is_roll`：滾奏／輪奏，兩者目前視為同一類。
- `duration`：可選的持續 tick，主要用於滾奏；不是另一個 Event。

數字譜規則：

- `0` 是休止，不建立 Event。
- 休止仍占時間；後續 Event 的 bar/tick 不可往前壓縮。
- 數字 `1..N` 只有在該樂器快照有明確 `pitch_N` 或 `drum_N` 等擊法時才可轉為 Event。

AI Event 可額外保存來源稽核欄位：

- `source_run_id`
- `source_candidate_id`
- `source_candidate_key`
- `source_evidence`

## 12. Recognition Run 與 Candidate

辨識工作基本結構：

```json
{
  "run_id": "RUN-...",
  "asset_id": "IMG-...",
  "segment_id": "BG0001",
  "region_id": "REG-...",
  "engine": "openai",
  "model": "gpt-...",
  "source_region": {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.2},
  "status": "ready_for_review",
  "error": null,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "candidates": [],
  "review_progress": {"pending": 0, "accepted": 0, "corrected": 0, "rejected": 0}
}
```

工作另可保存 request、模型設定、用量、成本、批次、嘗試次數與 `target_fingerprint`。`target_fingerprint` 用來確認 Segment、來源框、照片與設定是否仍與當次辨識一致。

候選：

```json
{
  "candidate_id": "C-...",
  "run_id": "RUN-...",
  "source_asset_id": "IMG-...",
  "kind": "event",
  "confidence": 0.82,
  "bbox": {"x": 0.2, "y": 0.3, "width": 0.02, "height": 0.03},
  "source_evidence": {
    "asset_id": "IMG-...",
    "region_id": "REG-...",
    "observed_text": "6"
  },
  "data": {},
  "review_status": "pending",
  "review": null,
  "created_at": "ISO-8601"
}
```

允許 `kind`：

- `event`
- `instrument`
- `stroke`
- `repeat`
- `barline`
- `text`
- `other`

審核狀態：

- `pending`
- `accepted`
- `corrected`
- `rejected`

修正後資料放在：

```json
{
  "decision": "corrected",
  "corrected_data": {},
  "note": "",
  "reviewed_at": "ISO-8601"
}
```

正式套用時，`corrected_data` 優先於 AI 原始 `data`。原始資料保留供稽核。

## 13. AI Event 候選資料

```json
{
  "instrument_id": "ins_...",
  "stroke_id": "stk_...",
  "bar": 1,
  "tick": 0,
  "duration": null,
  "is_accent": false,
  "is_ornament": false,
  "is_roll": false,
  "raw_symbol": "6",
  "note": null
}
```

- `bar` 是 Segment 局部小節號。
- 候選 `tick` 是小節內 tick。
- `instrument_id` 與 `stroke_id` 必須存在於該次 request 的快照，否則用 `unknown`。
- 只有 `accepted`／`corrected` 候選可轉入正式資料。
- 缺少樂器、缺少擊法、超出小節範圍、同格碰撞等情況都必須顯示為未套用理由。

## 14. 反覆與不同結尾

現行正式 navigation 放在 Segment 內：

```json
{
  "markers": [
    {"type": "repeat_start", "bar": 2},
    {"type": "repeat_end", "bar": 4, "times": 2},
    {"type": "volta", "start_bar": 4, "end_bar": 4, "passes": [1]},
    {"type": "volta", "start_bar": 5, "end_bar": 5, "passes": [2]}
  ],
  "playback_order": [],
  "review_status": "manually_reviewed"
}
```

規則：

- bar 皆為 Segment 內局部小節號。
- `repeat_end.times` 目前允許 2–32。
- 第一／第二結尾用 `volta.passes` 表示。
- Event 仍只存在紙面小節一次；播放時產生 `{source_bar, pass_index}` 的展開順序。
- 巢狀、重疊、不成對反覆或孤立 volta 會被拒絕。

AI repeat 候選使用相同概念，但必須先接受／修正，才可加入正式 `navigation.markers`。

**尚未確認的設計問題：**現行 navigation 只屬於單一 Segment，無法清楚表示跨 Segment 的反覆。vNext 在實作反覆 UI 前，必須決定是否新增 piece／score 層級的 navigation。

## 15. 正式套用的門檻

辨識候選轉為正式資料時，至少須符合：

1. 工作仍對應目前的 Segment、來源框與照片，不是 `stale`、`failed` 或 `superseded`。
2. 候選已 `accepted` 或 `corrected`。
3. Event 的 `instrument_id`、`stroke_id` 可對應樂器快照。
4. bar/tick 在 Segment 與該小節範圍內。
5. 同一格沒有無法解決的事件碰撞。
6. 同一 Segment 中目標樂器只對應一個 Track，否則人工選擇。
7. 已套用過的 `source_candidate_key` 不可重複套用。

套用結果必須分成：

- 可套用項目；
- 已套用項目；
- 略過項目與逐項原因；
- 衝突項目與修正入口。

現行試用中「只有第一段 1–9 小節及定音鼓進格」表示這個門檻目前沒有被使用者充分看見；vNext 必須把報告做成主要流程，不可只顯示模糊錯誤。

## 16. 匯出

現行可匯出：

- `segments.csv`
- `tracks.csv`
- `takes.csv`
- `events.csv`
- `dataset.jsonl`

Event 與 JSONL 主要使用每個 Track 的 `selected_take_id`。因此匯出前必須明示：

- 哪個 Take 被選取；
- AI Take 是否已人工確認；
- navigation 是否展開；
- Track 是否完整。

## 17. 不可混為一談的三種資料

| 層級 | 用途 | 可否直接播放／匯出為正式節奏 |
|---|---|---|
| Score Plan / Segment Proposal | 規劃頁序、小節、段落 | 不可 |
| Recognition Run / Candidate | AI 原始觀察與人工審核 | 不可 |
| Track / selected Take / Event / Navigation | 正式音樂資料 | 可以 |

下一版的兩個程式就是沿這條界線拆分。

