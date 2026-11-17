---
name: lipsync-shorts
description: 把「邊放原曲邊拍」的多個對嘴 take 自動剪成 YouTube Shorts／Reels：用相機收音對齊原曲、依拍點切換 take、可指定段落用哪個 take、.lrc 歌詞燒成字幕、輸出 1080x1920。使用者說「對嘴」「對嘴唱歌」「對嘴影片」「lip sync」「演梗」「把這幾個 take 剪成 Shorts」「卡點切換」「歌詞字幕」「嘴型對不上」時使用。不處理事後換配音的 AI 嘴型生成。
---

# Lipsync Shorts

全片以**原曲**為唯一時間基準與唯一音軌；每個 take 先對齊原曲，再在拍點上切換，嘴型就不會跑掉。
執行檔：`scripts/lipsync_shorts.py`（Python 3.9+、numpy、ffmpeg/ffprobe 含 libass）。

## 1. 收集輸入（缺什麼就問）

| 項目 | 必要 | 說明 |
|---|---|---|
| 原曲音檔 | ✅ | mp3/wav/m4a 皆可 |
| take 影片 | ✅ | 1 支以上，**必須有相機收音**（拍攝時有播原曲）；沒有收音只能用 `--offset` 手動指定 |
| 使用原曲哪一段 | ✅ | `--start`／`--end`（秒或 `1:02`）。演梗建議 13–25 秒；超過 60 秒提醒版權音樂可能被限制 |
| 歌詞 .lrc | 選用 | 時間以完整原曲為準；沒有就不燒字幕，或請使用者提供 |
| 指定段落 | 選用 | 例：「副歌用正面特寫」→ `--assign 1:05-1:15=a.mp4` |
| 切換速度 | 選用 | `--beats-per-cut`：預設 4；快節奏 2、抒情 8 |

先檢查環境：`ffmpeg -version`、`python -c "import numpy"`；字型用系統內有的中文字型
（Windows `Microsoft JhengHei`、macOS `PingFang TC`、Linux `Noto Sans CJK TC`），用 `fc-list :lang=zh family` 確認。

## 2. 先跑計畫，不出片

```bash
python scripts/lipsync_shorts.py --song SONG --take A --take B --start S --end E --out OUT.mp4 --plan-only
```

讀 `OUT.plan.json` 逐項核對，**任何一項不對就不要出片**：

1. **信心值**（`takes[].confidence` ＝ 最佳峰值 ÷ 次佳峰值）：低於 1.15 腳本會自行停止；1.15–1.3 要特別小心。
2. **整拍誤對**：最常見的錯。若懷疑某 take 對錯，算 `60 / bpm` 拍長；
   兩個 take 的 offset 差、或 offset 與使用者描述（「我從副歌前兩句開始拍」）的落差，
   接近拍長的整數倍就很可能是對錯拍 → 請使用者說出該 take 大概從原曲哪裡開始，用 `--offset TAKE=秒數` 指定。
   定義：原曲秒數 = take 秒數 + offset。
3. **涵蓋範圍**（`vfrom`–`vto`）：要用的段落至少要有一個 take 涵蓋；`--assign` 的 take 必須涵蓋指定區間。
4. **片段**：數量、長度、`assigned` 標記是否符合使用者要求。

## 3. 出片

```bash
python scripts/lipsync_shorts.py --song SONG --take A --take B --start S --end E \
  --lrc LYRICS.lrc --font "字型名稱" --out OUT.mp4 [--assign FROM-TO=TAKE ...]
```

出片後自行驗證再回報：
- `ffprobe` 確認 1080x1920、30fps、影音長度一致；
- 抽 2–3 張有歌詞的畫面（`ffmpeg -ss T -i OUT.mp4 -frames:v 1 f.png`）確認字幕位置、折行、字型沒有變成方框；
- 告訴使用者：嘴型要用人眼看「ㄅㄆㄇ」閉唇音，機器只能保證對齊原曲。

## 4. 使用者回報「嘴型對不上」

| 症狀 | 處理 |
|---|---|
| 全片都差一點點 | `--nudge-ms`，每次 ±33（一格）。正值＝畫面往後延（聲音比嘴早時用正值） |
| 只有某個 take 錯，而且差很多（約整拍） | 該 take 對錯拍 → `--offset` 手動指定 |
| 某個 take 裡面越來越不準 | 拍攝時播放器卡頓或變速 → 換掉該 take（不要用 `--assign` 指定它）或重拍 |
| 某一段沒有 take 可用而報錯 | 調整 `--start/--end`，或請使用者補拍 |

## 參數速查

`--assign 1:05-1:15=a.mp4`（或 `=2` 表示第 2 個 `--take`；可重複；區間內只用該 take，邊界離拍點 `--snap` 秒內會吸到拍點）、
`--beats-per-cut`、`--min-seg 0.8`、`--offset TAKE=SEC`、`--nudge-ms`、`--font`、`--font-size 78`、
`--line-chars 12`（單行字數上限，超過自動折兩行）、`--lyric-lead 0.1`、`--min-confidence 1.15`、`--fps 30`、`--crf 18`、`--keep-temp`。

## 拍攝建議（使用者還沒拍時告訴他）

1. 邊用喇叭或耳機播原曲邊拍，相機收音開著。
2. 每個 take 至少錄 5 秒以上原曲，最好整段副歌；所有 take 都涵蓋要用的段落最理想。
3. 開頭拍一下手可以幫助人工核對。
4. 版權：自行上傳含商業歌曲的影片可能被 Content ID 認領；用 YouTube App 的 Shorts 音樂庫較安全，但可能限制片長。歌詞本身也有版權。

## 限制

- 拍點偵測是簡易版，節奏自由或沒有鼓的歌，切點可能不在拍上（改用 `--assign` 手動定切點）。
- 未指定段落的 take 是輪流使用。
- 不做 AI 嘴型生成；拍壞了建議重拍。
