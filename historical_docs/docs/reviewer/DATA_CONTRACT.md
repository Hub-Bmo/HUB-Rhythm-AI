# 程式 B 第一版資料契約

## 所有權

- A 的 `recognition-package.json`（或匯出 zip）唯讀。B 不呼叫 `Workspace.load`，避免把正在辨識的 run 標為中斷。
- B 自己的 `review-project.json` 保存 `format=musicai-score-review`、`schema_version=1.0`、`ppq=960`。
- `source_package_id` 限定同一首作品，`imports` 記錄來源 revision 與讀取時間；`regions` 保存來源 run 與狀態。
- `assets` 為複製到 B 的照片；音效存於 `assets/sounds/`。路徑為專案內相對路徑，經過邊界驗證。

## 候選與人工決定

每筆 `entries` 包含：

- `id`：原 observation_id；手動補入使用獨立 MANUAL ID。
- `run_id`、`region_id`：來源識別碼。
- `original`：原始辨識觀察快照；手動新增為 null。
- `value`：可編輯的副本，時間、音高、樂器／擊法、修飾及來源框全部保留。
- `status`：pending、accepted、rejected。
- `acknowledged`、`review_note`：人工核對決定與備註。

刪除以 status=rejected 表示，exclusion_reason 保存原因，exclusion_history 記錄一批 entry_ids 及時間。刪除不要求先補齊該筆資料，不覆寫 original、修正值或原有 review_note；預設從節奏格與清單隱藏，不進試聽／訓練 events。復原整批或選取項目回到 pending，不自動重新接受。整列刪除按目前小節的 staff_label 選取，整譜名稱刪除按 score_sound_key 選取當前已匯入資料，並非對未來 run 的永久黑名單。

相同 run 再匯入不覆蓋人工決定。另一個 run 取代時，舊項目含修正保存至 `archived_entries`，新候選重新待核對。來源變成失敗或過期不能沿用舊成功結果。

`onset`、`duration` 為非負有理數 `{n,d}`，單位為四分音符；時值必須大於零。編輯介面的「第幾拍」從 1 起，資料的 onset 從 0 起。匯出檢查 960 PPQ 可精確表示性。

`pitches` 保留音級、相對八度、譜面文字、譜號與升降資料；`midi` 為實際音高。已知 `1=G` 但未知基準八度仍不足以換算。使用者選定完整基準音，且該音的音級／相對八度均已知時才換算。未知不能以 0 或固定中音取代。

樂器與技法分開使用 `instrument_id`／`technique_id`；原名稱、原符號可查閱。使用者新建名稱以固定 namespace 的 UUID5 產生穩定 ID，同名可跨新版專案保持一致。舊庫既有樂器 ID 保留；此版不回寫舊版全域主庫。

`name_corrections` 保存使用者確認的全譜名稱規則；修改 value 的正式名稱／身分，original 與 A 的 raw_output 不改。例軍鼓→軍鈸也修正共用列中的名稱 token，但不因此猜定共用列每一擊的身分。新 run 匯入在保存 original 後套用規則。受影響的非 rejected 觀察重新 pending；rejected 不會重開。音色覆寫按名稱遷移，不覆蓋既有目的名稱的音色設定。

「依名稱整理樂器」對單一可識別名稱建立獨立身分；從泛稱鼓／鈸等音色庫對應整理為原譜樂器時，保留已有技法及樣本，未知技法保持 null。不將組合列或「第5列、原文疑…」的暫名猜成單一樂器。

`score_sounds` 是本譜的試聽音色覆寫表，以來源 instrument_text（缺少時用 staff_label）的規整名稱為鍵，值是 SOUND_PROFILES 的音色代碼。只統一換行與逗號／斜線等分隔符，不猜同義字、不拆開身分未明的共用聲部。清單包含尚未排除的事件與休止，排除純樂器庫、封存及全數排除的名稱。

本譜下拉選單自動保存覆寫；播放 plan 優先使用所選內建音色且暫不使用該列既有 sample，清除覆寫後恢復原音效／樣本。覆寫不改事件的名稱、instrument_id/technique_id、音高、接受狀態、原候選或樂器庫；未知身分仍須核對後才可成為訓練資料。此表隨 B 專案保存及增量匯入保留，屬於試聽設定，不新增訓練事實。

## 小節與段落

`bars` 使用整首紙面小節號，來源框、頁碼、逐小節拍號／速度與共通補充值分開保存。變拍號不靠固定 4/4 推算。一般範圍段落的起訖可跨頁，彼此不重疊，段落編輯保持 segment_id。

跳轉段落使用 `kind=sequence`，以 `ranges=[{start_bar,end_bar},...]` 保存人工明寫的演奏順序，`sequence_acknowledged` 表示順序是否已核對。每組範圍奏一次，能返回或重用其他段落中的紙面小節；不額外套用全曲導航。其 start_bar/end_bar 僅為第一組開始／最後一組結束，可能起點大於終點，不能作為紙面聯集範圍。實際包含的小節必須從 ranges 求聯集。修改順序後重新待確認。

例 `153–154 → 1–8 → 1–7 → 9–17` 包含 26 個演奏引用、19 個唯一紙面小節；8 只有第一次出現，重複的小節引用相同原事件。可先保存／試聽未確認段落，訓練匯出須已確認順序且每個來源小節通過原有事件核對門檻。

full_rest 有獨立候選，休止不產生敲擊。來源 failed/running/draft/empty 與已確認全休止不同；未讀清楚不得默認靜音進入訓練資料。

## 反覆

`navigation_candidates` 保留 A 原候選；`navigation_draft` 和 `navigation_draft_scope` 保存編輯中清單與紙面範圍。`navigation` 保存人工確認記號，`navigation_scopes` 明確記錄已核對範圍；`navigation_confirmed` 表示是否涵蓋整首。修改草稿或匯入新的相關候選會使受影響範圍重新待確認。播放與匯出檢查實際範圍覆蓋，不能把確認 1–9 當成整首無其他反覆。舊版只有整首確認旗標的專案在讀取時補入對應範圍。

記號沿用純邏輯導航格式：repeat_start / repeat_end 以 bar 定位，volta 以 start_bar / end_bar / passes 定位。演奏展開只建立 source_bar / pass_index 的引用，不能複製原事件。其他跳轉候選不被靜默刪掉或匯出。

B 另支援一組一次性的 `dc` 或 `dc_coda`：

```json
{"type":"dc_coda","bar":154,"until_bar":17,"target_bar":155,"repeat_policy":"skip"}
```

`bar` 為 D.C. 指令所在小節（奏完才返回 1），`until_bar` 為返回後奏完才轉向尾奏的小節，`target_bar` 為尾奏第一小節。`repeat_policy` 必填：`play` 重做一般反覆；`skip` 不重做且採最後一次結尾，不能把不同結尾全部串起來。單純 `dc` 沒有 until_bar/target_bar，返回一次後演奏至末尾。D.S.、Fine、多組返回、切斷反覆或結尾中途的路線仍不支援，必須報錯。

有返回時，展開引用增加 `route_phase`：0 首次演奏、1 返回後、2 Coda 尾奏。確認範圍必須包含 1、D.C. 及 Coda 目的地；局部編輯不能刪除已確認跨界跳轉。試聽與匯出使用相同路線展開器。跳轉處不會把相同音高的延音線直接接起來。

「預覽草稿路線」顯示全部小節，不將超長清單截斷後藏起跳轉；「依草稿試聽」只在記憶體副本上使用草稿，不寫入已確認導航。原始 other 候選保留，只有原文明確為 D.C./To Coda/Coda 符號且位置吻合人工確認路線時，才視為已處理；其他未知跳轉仍阻擋匯出。Coda 配對與返回後反覆不由辨識模型自行猜測。

## 試聽與訓練匯出

試聽可以包含 pending 候選，並清楚列出缺音高、缺時間、略過等提醒。可選擇以 C3 作簡譜 1 預覽相對音高，僅當音級、相對八度及升降資料可判定時使用；已知與未知實際音高混合的和弦不以不同基準混配。這些暫時音高只存在播放 plan（relative_pitch_preview），不寫回事件；完整標註模式要求確認實際 MIDI，節奏優先模式可保存未知而不作為音高訓練目標。輪奏每秒 20 次、裝飾音前倚音等是預覽設定，不是訓練事實。單次音訊最多 10 分鐘，長作品可分範圍。

`review_mode` 為 complete（舊專案預設、保留原門檻）或 rhythm（使用者在核對助手選定）。rhythm 允許未知 technique_id、實際 MIDI、輪奏／重音／裝飾音及 tie，但不允許非法值、缺樂器或缺時值。`acceptance_profile` 記錄該筆接受時使用的標準；切回 complete 後匯出仍會按較嚴格標準重驗。

「全部套用基本擊法」是使用者主動指定整譜未對應擊法採「一般敲擊」：僅修改未排除、kind=note、已有有效 instrument_id 且 technique_id 未對應的事件。保留既有有效擊法、原候選、人工備註與其他演奏欄位；變動筆追加操作備註並設為 pending／acknowledged=false。新增的有效擊法對應使 technique 遮罩為 true，其他未知欄位仍保留原遮罩；此操作不等於接受資料，也不自動套用到日後新匯入的候選。

rhythm 的個別 rest 允許 onset/duration 未知，因休止本身不產生敲擊；已知數字仍須合法且不超出小節。rest_observations 增加 timing_known 與 training_mask.interval，只有 full_rest 或位置／時值均明確的休止能當作定位休止證據。未定位休止保留 null，不能任意填入某拍、推成全小節無聲或為通過驗證而刪除。

匯出帶 `annotation_policy`，每筆事件帶 `training_mask`，分別標示 instrument、onset、duration、technique、pitches_midi、pitches_degree、is_roll、is_accent、is_ornament、tie、attack、dynamic、slur。未知 bool 保留 null 且 mask=false，確定的 false 則 mask=true；不得把未知改成陰性標註。attack 是由已知 tie 派生：none/start 為 true，continue/stop 為 false，未知則 null；用於敲擊目標時必須遵守其 mask。訓練器必須支援此遮罩契約，不能忽略 mask 後將 null 補成 false／0。

批次核對只接受來源成功、拍號速度與核心欄位有效的 pending 項目，仍需使用者勾選已核對範圍。問題依小節彙整，其他項目留 pending；全數排除或來源沒有候選不能當全休止。節奏優先的預覽軟提醒不再阻擋訓練匯出，已知延音不連續／未閉合仍屬 blocking_warnings。

訓練輸出 `format=musicai-reviewed-training`、`schema_version=1.0`，包含：

- 作品／來源識別碼與匯出時間。
- `segments`：人工建立的段落。
- `instruments`：樂器與擊法定義、校對音效設定。
- `navigation`：整首已確認記號（可引用段落外小節）；`playback_order` 只保留本次有匯出事件／休止的小節引用。
- `bars`：逐小節拍號／速度、來源、accepted 敲擊 events、已接受休止的 rest_evidence ID、人工排除的 observation ID。
- `excluded_bar_numbers`：登記於 B 但不在本次訓練段落中的小節。

每個輸出段落增加 `event_count`。統計非 rejected 的 note 起音事件，依已確認導航／自訂順序計入重奏次數；tie=continue/stop 不算新敲擊，未知 tie 計為候選並顯示起音待確認。輪奏不拆次、和弦不按音高倍增。介面另外顯示來源缺漏與 pending 狀態，未確認路線按紙面估計；0 不等於已確認休止。確認全段休止須每個小節來源成功、觀察已接受，且各來源聲部有 full_rest 或足夠覆蓋全小節的休止區間。

輸出段落另帶 verified_silent。event_count=0 但 verified_silent=false 不能視為全段無聲標註，尤其存在未定位／不足覆蓋小節的休止時。

`rest_evidence` 保留相容的 ID 清單，新增 `rest_observations` 明寫 evidence_id、kind、staff_label、instrument_text、instrument_id、onset、duration 及來源定位。scope=source_staff 表示證據針對原譜聲部；組合列的 instrument_id 未確認時保持 null，不推論為同時敲擊兩件樂器或新增混合樂器。full_rest 的 onset/duration 可為 null，整小節長度由 bars.time_signature 決定。

移除段落後，匯出僅使用保留段落的小節聯集；某小節若仍被另一段引用，就保留一次原事件及對應演奏引用。僅刪段落不會刪原辨識資料。

若包含跳轉段落，輸出增加 `playback_order_mode=segments`，每個 segments 項目都帶自己的 playback_order。sequence 的引用為 `{source_bar,sequence_step}`，sequence_step 是第幾組範圍（從 1 起），不猜一般反覆的 pass_index。頂層 playback_order 依段落清單順序串接，引用帶 segment_id，不能當成原曲全曲路線；各段須依各自 playback_order 作為訓練序列。bars 中每個紙面小節／事件仍只存一次。只有一般段落時維持原有輸出語意。

自訂順序的人工確認取代該段對全曲 navigation_scopes 的依賴；不代表全曲導航已確認，也不改寫原候選或全曲反覆。混合匯出時，一般段落仍要求 navigation_scopes 及未支援跳轉檢查。延音與時值檢查使用各自實際順序，不能用最小到最大小節範圍代替，避免把未選的 18–152 一併帶入。

事件帶 original observation_id、run_id 及來源框，人工修改後的時間分數是權威值，不輸出原辨識已過期的 tick 快取。正式事件狀態為 accepted，原辨識警告留在校對專案而非當作未解決訓練欄位。

匯出前驗證每個段落的來源成功、拍號與速度、所有候選已決定、樂器、時間範圍、反覆確認與已知延音連接；complete 額外要求擊法、實際音高及演奏旗標完整，rhythm 則保留未知與訓練遮罩。失敗為整次停止並列出原因，不輸出看似完整的部分資料。同聲部的全休止與敲擊不能同時接受；整小節全部排除也不能算確認休止。

WAV 是可重建的試聽衍生物，預覽檔不屬於訓練資料；自訂採樣本身的聲音不代表模型要學習的音色。新版 JSON 尚未對接模型的 tokenizer 或 legacy Track / Take CSV。

## 閱讀用表格

`ReviewDocument.training_payload()` 集中所有匯出檢查並產生快照；JSON 與 `export_tables()` 共用，不由表格另行放寬門檻。表格輸出不修改 live manifest。來源、專案及 assets 的覆寫保護共用。

Excel `.xlsx` 與 CSV `.zip` 以同一 `make_tables()` 模型建立六表：段落統計、原譜事件、演奏事件、休止紀錄、演奏順序、欄位說明。原譜事件只列一次；演奏事件使用每段 playback_order（舊一般段落則從全曲 order 篩選），保留重奏、段落重用與自訂路線。

統計分開 attack=true、null、false；前兩項加總為含待確認的候選擊打數，false 為延音延續。原譜事件表及演奏事件表都包含延音事件，不應把列數當作實際敲擊下數。未知 bool 顯示未知，training_mask 原值以 JSON 字串保留。時間同時提供從 1 起的數值拍點及精確分數文字，源 JSON onset 仍從 0 起；休止未定位不補值。

XLSX 數字為數值儲存格，分數／原符號為 inlineStr，含篩選與凍結表頭。CSV 每表 UTF-8 BOM，公式形狀的文字加單引號避免被試算表執行；分數／拍號亦加單引號，避免直接開啟被轉成日期。統計用數值欄仍為數字。兩格式一次性原子寫檔，錯誤不覆蓋既有匯出。表格只供檢查與統計，無回寫／訓練器介面承諾。
