# 交接包驗證結果

這個目錄保存2026-10-30在原Windows／RTX4060Ti電腦上重新執行的檢查。尚未在另一台電腦安裝環境測試。

- `data_rebuild_from_midi.json`：從原GMD MIDI重建全部token，含國樂、V2切分及V3映射的完整比對結果。
- `data_rebuild_reviewed_json.json`：再次從校對JSON核對工作庫並重建國樂、V2及V3。
- `reviewed_json_matches_library.json`：六首校對JSON與工作庫payload逐項相同。
- `01-midi-convert.log`、`02-tokenize.log`、`03-vocab.log`：GMD重建輸出。
- `unit_tests.log`：27項測試，全部通過，無跳過項。
- `smoke_summary.json`、`smoke/`：三階段GPU檢查、V2續訓、V1 CPU檢查的設定及log。每輪最多兩個batch，這些結果不能作為正式模型品質評估。
- `guoyue_transformer_ft_v1-verification.json`、`guoyue_gmd12_ft_v1-verification.json`、`current_comparison.json`：正式V2／V3的重新評估。
- `v2-verification.json`：確認小型V2續訓後，仍保留它最初使用的小型V1來源紀錄；這是檢查用模型。
- `cpu_generation.json`及`sample.*`：使用正式V1在CPU產生一小節的實測結果。

報告中的當時絕對路徑為執行紀錄。大型work與小型訓練權重已清理，因此這些暫存路徑不代表交接包內仍有該檔案。正式權重在outputs，隨時可依reproduce.py重新執行驗證。

完整性清單在根目錄package_manifest.json。下載後執行`python reproduce.py verify --models`會在verification/integrity.json產生該台電腦的新核對結果。
