# lipsync-shorts：對嘴 YT Shorts 自動剪輯

自動完成三件事：
1. **對齊**：把每個 take 對齊到原曲；
2. **拍點切換**：在拍點上切換 take；
3. **出片**：把 .lrc 歌詞燒成字幕，輸出 1080×1920 的 Shorts。

## 需求
- Python 3.9+、`numpy`（`pip install numpy`）
- `ffmpeg`／`ffprobe`，要支援 libass，一般發行版都有
- 中文字型，例如 Noto Sans CJK TC（Windows 可用 `--font "Microsoft JhengHei"`）

## 拍攝規則（決定能不能自動對齊）
1. 邊用喇叭或耳機播原曲邊拍，**相機收音要開**。
2. 每個 take 至少錄到原曲 5 秒以上，最好整段副歌。
3. 鏡頭只要涵蓋你要用的段落；某段沒有任何 take 拍到，腳本會停下來告訴你。

## 用法
```bash
python lipsync_shorts.py --song song.mp3 --take a.mp4 --take b.mp4 --take c.mp4 --start 62 --end 82 --lrc song.lrc --out short.mp4
```

指定段落用哪個 take（例如副歌 1:05–1:15 用正面特寫 a.mp4，結尾 1:18–1:22 用第 3 個 take），其餘段落照拍點自動輪流：
```bash
python lipsync_shorts.py --song song.mp3 --take a.mp4 --take b.mp4 --take c.mp4 --start 62 --end 82 --lrc song.lrc --out short.mp4 --assign 1:05-1:15=a.mp4 --assign 1:18-1:22=3
```
規則：
- 指定區間的頭尾一定是剪接點，區間內**整段只用那個 take**，不會再自動切換；
- 自動段落會盡量避開前一段和下一段指定用的 take，讓切換看得出來；
- 如果只剩同一個 take 可選，它會和相鄰的指定段落接成一段；
- 指定的 take 沒拍到那段時間，會直接報錯，告訴你它實際拍到哪一段；
- 區間重疊、檔名不存在、編號超出，都會在對齊前就報錯，不必等分析跑完。

先只看計畫、不出片，確認每個 take 的偏移和片段安排（寫到 `short.plan.json`）：
```bash
python lipsync_shorts.py --song song.mp3 --take a.mp4 --take b.mp4 --start 62 --end 82 --out short.mp4 --plan-only
```

| 參數 | 說明 |
|---|---|
| `--start` / `--end` | 使用原曲哪一段（秒）。演梗類建議 13–25 秒 |
| `--beats-per-cut 4` | 每幾拍換一個 take；2 比較快、8 比較慢 |
| `--assign 1:02-1:10=b.mp4` | 指定原曲某段用哪個 take，可重複。take 可寫檔名或編號（`=2` 表示第 2 個 `--take`）；時間可寫 `72` 或 `1:12`，以完整原曲為準 |
| `--snap 0.15` | `--assign` 的邊界離拍點 0.15 秒內就吸附到拍點 |
| `--offset b.mp4=12.34` | 自動對齊錯時手動指定。定義：原曲秒數 = take 秒數 + offset |
| `--nudge-ms 33` | 嘴型整體微調；正值表示畫面往後延，30fps 下一格約 33 ms |
| `--font` / `--font-size` / `--line-chars` | 字幕字型、大小、單行字數（超過自動折兩行） |
| `--lyric-lead 0.1` | 字幕比唱早出現幾秒 |
| `--min-confidence 1.15` | 對齊信心門檻（最佳峰值 ÷ 次佳峰值），低於就停止 |

## 嘴型對不上時怎麼辦
1. 打開 `.plan.json`，看每個 take 的 `offset`。如果某個 take 差了整數拍（例如剛好 0.5 秒或 1 秒的倍數），就是對錯位置了，改用 `--offset` 手動指定。
2. 全片都差一點點：看「ㄅ、ㄆ、ㄇ」這類閉嘴唇的音，用 `--nudge-ms` 每次調 ±33 ms 再試。
3. 只有某一段不準：通常是那個 take 拍攝時播放器卡頓，換掉那個 take 或重拍。

## 已知限制
- 拍點偵測是簡易版：節奏很自由、或沒有鼓的歌，切點可能不在拍上。
- 沒有用 `--assign` 指定的段落，take 是輪流使用的。
- 歌詞要自己準備 .lrc，時間以完整原曲為準。
- 使用商業歌曲會有版權認領風險，詳見上一份說明。

## 安裝成 Claude Code skill

### Windows（PowerShell）
1. 安裝工具（已經有的會略過）：
   ```powershell
   winget install -e --id Python.Python.3.12
   winget install -e --id Gyan.FFmpeg
   winget install -e --id Git.Git
   ```
   裝完**關掉 PowerShell 重開**，讓新的 PATH 生效，再安裝 numpy：
   ```powershell
   python -m pip install numpy
   ```
   `python` 如果跳出 Microsoft Store，改用 `py -m pip install numpy`。
2. 下載 skill 到 Claude Code 的 skills 資料夾：
   ```powershell
   git clone --depth 1 https://github.com/JoyceNien/claude-skill-lipsync-shorts "$env:TEMP\lipsync-skill"
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
   Copy-Item -Recurse -Force "$env:TEMP\lipsync-skill\lipsync-shorts" "$env:USERPROFILE\.claude\skills\"
   Remove-Item -Recurse -Force "$env:TEMP\lipsync-skill"
   ```
3. 檢查：
   ```powershell
   ffmpeg -version
   python -c "import numpy; print('numpy OK')"
   python "$env:USERPROFILE\.claude\skills\lipsync-shorts\scripts\lipsync_shorts.py" --help
   ```
4. 重開 Claude Code，說「對嘴」測試。字幕字型請用 `--font "Microsoft JhengHei"`（微軟正黑體）。

更新：重跑第 2 步即可。

### macOS／Linux
把 `lipsync-shorts/` 資料夾複製到 `~/.claude/skills/lipsync-shorts/`，並安裝 Python 3.9+、`pip install numpy`、ffmpeg。
