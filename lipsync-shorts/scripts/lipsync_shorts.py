#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lipsync_shorts.py — 對嘴唱歌／演梗 YT Shorts 自動剪輯

流程：
  1. 自動對齊：用相機收到的參考音（邊放原曲邊拍）對原曲做互相關，算出每個 take 的時間偏移
  2. 拍點切換：偵測原曲拍點，每 N 拍換一個 take（只挑那段時間有畫面的 take）
  3. 出片：原曲當唯一音軌、.lrc 歌詞轉字幕燒入，輸出 1080x1920 Shorts

需求：Python 3.9+、numpy、ffmpeg/ffprobe（含 libass）。

範例：
  python lipsync_shorts.py --song song.mp3 --take a.mp4 --take b.mp4 \
      --start 62 --end 82 --lrc song.lrc --out short.mp4

先只看計畫不出片：加 --plan-only（會寫出 short.plan.json）。
自動對齊錯了：用 --offset b.mp4=12.34 手動指定（原曲秒數 = take 秒數 + offset）。
嘴型整體差一點：用 --nudge-ms 微調（正值＝畫面往後延；一格 30fps ≈ 33ms）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

for _stream in (sys.stdout, sys.stderr):  # Windows 主控台預設 cp950，中文/符號會讓 print 當掉
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ALIGN_SR = 8000          # 對齊用取樣率
ALIGN_HOP = 80           # 10 ms 一格
BEAT_SR = 11025
BEAT_HOP = 128           # ≈12 ms 一格
W, H = 1080, 1920


def die(msg: str) -> None:
    print("錯誤：" + msg, file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


# ───────────────────────────── 媒體讀取

def probe(path: str) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "format=start_time,duration:stream=codec_type,start_time,duration",
               "-of", "json", path], capture_output=True, text=True).stdout
    info = json.loads(out)
    fmt_start = float(info["format"].get("start_time") or 0.0)
    fmt_dur = float(info["format"].get("duration") or 0.0)
    res = {"fstart": fmt_start, "duration": fmt_dur, "astart": None, "vstart": None, "vdur": None}
    for s in info.get("streams", []):
        st = s.get("start_time")
        st = float(st) if st not in (None, "N/A") else fmt_start
        if s.get("codec_type") == "audio" and res["astart"] is None:
            res["astart"] = st
        if s.get("codec_type") == "video" and res["vstart"] is None:
            res["vstart"] = st
            d = s.get("duration")
            res["vdur"] = float(d) if d not in (None, "N/A") else fmt_dur - (st - fmt_start)
    return res


def load_audio(path: str, sr: int) -> np.ndarray:
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
               "-f", "f32le", "-"], capture_output=True).stdout
    y = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    if y.size == 0:
        die(f"{path} 讀不到音訊（take 必須有相機收音才能自動對齊，或改用 --offset）")
    return y


def _frames(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    if y.size < n_fft:
        y = np.pad(y, (0, n_fft - y.size))
    n = 1 + (y.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    return y[idx] * np.hanning(n_fft)[None, :]


def onset_envelope(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """對數頻譜通量：對麥克風音色、音量差異比原始波形穩定。"""
    out = []
    step = 4096  # 分批避免大矩陣
    prev = None
    fr_all = _frames(y, n_fft, hop)
    for i in range(0, len(fr_all), step):
        mag = np.log1p(10.0 * np.abs(np.fft.rfft(fr_all[i:i + step], axis=1)))
        if prev is not None:
            mag_full = np.vstack([prev[None, :], mag])
        else:
            mag_full = np.vstack([mag[:1], mag])
        flux = np.maximum(0.0, np.diff(mag_full, axis=0)).sum(axis=1)
        out.append(flux)
        prev = mag[-1]
    return np.concatenate(out)


def _z(x: np.ndarray) -> np.ndarray:
    s = x.std()
    return (x - x.mean()) / (s if s > 0 else 1.0)


def _parabolic(c: np.ndarray, k: int) -> float:
    if 0 < k < len(c) - 1:
        a, b, d = c[k - 1], c[k], c[k + 1]
        den = a - 2 * b + d
        if den != 0:
            return k + 0.5 * (a - d) / den
    return float(k)


# ───────────────────────────── 1. 自動對齊

def band_features(y: np.ndarray) -> np.ndarray:
    """多頻帶對數能量（去掉慢變化）：保留旋律/音高資訊，避免週期性節拍造成整拍誤對。"""
    n_fft = 1024
    edges = np.geomspace(120, 3600, 33)
    freqs = np.fft.rfftfreq(n_fft, 1 / ALIGN_SR)
    bands = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges, edges[1:])]
    fr = _frames(y, n_fft, ALIGN_HOP)
    feats = []
    for i in range(0, len(fr), 4096):
        mag = np.abs(np.fft.rfft(fr[i:i + 4096], axis=1)) ** 2
        feats.append(np.stack([np.log1p(1e3 * mag[:, m].sum(axis=1)) for m in bands], axis=1))
    f = np.vstack(feats)
    k = 50  # 0.5 秒移動平均當基線
    base = np.apply_along_axis(lambda c: np.convolve(c, np.ones(k) / k, "same"), 0, f)
    f = f - base
    f -= f.mean(axis=1, keepdims=True)        # 每格去掉整體音量 → 留下頻譜形狀
    return (f - f.mean(axis=0)) / (f.std(axis=0) + 1e-9)


def _xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """多維互相關；回傳 (lags, 值)，lag k 表示 b 第 0 格對到 a 第 k 格。"""
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    acc = np.zeros(nfft)
    for j in range(a.shape[1]):
        acc += np.fft.irfft(np.fft.rfft(a[:, j], nfft) * np.conj(np.fft.rfft(b[:, j], nfft)), nfft)
    lags = np.concatenate([np.arange(0, len(a)), np.arange(-(len(b) - 1), 0)])
    vals = np.concatenate([acc[:len(a)], acc[nfft - (len(b) - 1):]])
    # 以重疊長度正規化，避免只重疊一小段的位置被高估／低估
    overlap = np.minimum(len(a), lags + len(b)) - np.maximum(0, lags)
    vals = np.where(overlap > 200, vals / np.maximum(overlap, 1), -np.inf)
    return lags, vals


def align_take(song_feat: np.ndarray, song_wave: np.ndarray, take_path: str) -> tuple[float, float]:
    """回傳 (offset 秒, 信心值)。原曲秒數 = take 音訊秒數 + offset。
    信心值 = 最佳峰值 ÷ 次佳峰值（排除最佳峰 ±0.3 秒）；越接近 1 越可能對錯。"""
    tw = load_audio(take_path, ALIGN_SR)
    lags, vals = _xcorr(song_feat, band_features(tw))
    k = int(np.argmax(vals))
    others = vals.copy()
    others[np.abs(lags - lags[k]) <= 30] = -np.inf
    second = float(np.max(others)) if np.isfinite(others).any() else 0.0
    conf = float(vals[k] / second) if second > 0 else float("inf")
    frac = _parabolic(np.nan_to_num(vals, neginf=0.0), k) - k
    offset = (lags[k] + frac) * ALIGN_HOP / ALIGN_SR

    # 精修：在 ±40ms 內用原始波形互相關（失敗就保留粗對齊）
    try:
        win = 10 * ALIGN_SR
        energy = np.convolve(tw ** 2, np.ones(ALIGN_SR), "valid")
        st = int(np.argmax(energy[: max(1, len(energy) - win)])) if len(tw) > win else 0
        seg = tw[st: st + win]
        guess = int(round((st / ALIGN_SR + offset) * ALIGN_SR))
        pad = int(0.04 * ALIGN_SR)
        lo, hi = guess - pad, guess + len(seg) + pad
        if lo >= 0 and hi <= len(song_wave) and len(seg) > ALIGN_SR:
            ref = song_wave[lo:hi]
            m = len(ref) + len(seg)
            mf = 1 << (m - 1).bit_length()
            cc = np.fft.irfft(np.fft.rfft(ref, mf) * np.conj(np.fft.rfft(seg, mf)), mf)[: 2 * pad + 1]
            norm = math.sqrt(float((seg ** 2).sum()) * float((ref ** 2).sum()) / len(ref) * len(seg)) or 1.0
            kk = int(np.argmax(cc))
            if cc[kk] / norm > 0.25:
                offset = (lo + _parabolic(cc, kk)) / ALIGN_SR - st / ALIGN_SR
    except Exception:
        pass
    return offset, conf


# ───────────────────────────── 2. 拍點偵測（Ellis 動態規劃）

def detect_beats(y: np.ndarray) -> tuple[float, np.ndarray]:
    env = onset_envelope(y, 1024, BEAT_HOP)
    env = env - np.convolve(env, np.ones(16) / 16, "same")
    env = np.maximum(env, 0)
    env = env / (env.std() or 1.0)
    fps = BEAT_SR / BEAT_HOP
    # 節拍：自相關 + 以 120 BPM 為中心的對數高斯權重
    nf = 1 << (2 * len(env) - 1).bit_length()
    spec = np.fft.rfft(env, nf)
    ac = np.fft.irfft(spec * np.conj(spec), nf)[:len(env)]
    lags = np.arange(len(ac))
    lo, hi = int(fps * 60 / 200), int(fps * 60 / 60)
    with np.errstate(divide="ignore"):
        weight = np.exp(-0.5 * (np.log2(lags / (fps * 0.5))) ** 2 / 0.9 ** 2)
    score = ac * np.nan_to_num(weight)
    period = lo + int(np.argmax(score[lo:hi]))
    bpm = 60 * fps / period
    # 動態規劃追拍
    alpha = 100.0
    n = len(env)
    cum = env.copy()
    back = -np.ones(n, dtype=int)
    prange = np.arange(-2 * period, -period // 2)
    txcost = -alpha * np.log(-prange / period) ** 2
    for t in range(2 * period, n):
        cand = cum[t + prange] + txcost
        j = int(np.argmax(cand))
        cum[t] = env[t] + cand[j]
        back[t] = t + prange[j]
    tail = cum[max(0, n - period):]
    t = max(0, n - period) + int(np.argmax(tail))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1], dtype=float)
    return bpm, (beats * BEAT_HOP + 1024 / 2) / BEAT_SR  # 以分析窗中心為時間


# ───────────────────────────── 3. 剪輯計畫

@dataclass
class Take:
    path: str
    offset: float
    confidence: float
    manual: bool
    corr: float           # 音訊起點 - 檔案起點
    vfrom: float          # 有畫面的原曲時間範圍
    vto: float


@dataclass
class Segment:
    song_from: float
    song_to: float
    take: int
    seek: float           # take 檔案內的 -ss（秒）
    frames: int
    assigned: bool = False  # 是否由 --assign 指定


@dataclass
class Assign:
    song_from: float
    song_to: float
    take: int


def parse_time(s: str) -> float:
    """接受 72.5、1:12.5、0:01:12.5。"""
    parts = s.strip().split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        die(f"時間格式錯誤：{s!r}（例：72.5 或 1:12.5）")
    out = 0.0
    for v in vals:
        out = out * 60 + v
    return out


def parse_assigns(items: list[str], take_paths: list[str]) -> list[Assign]:
    """--assign 1:02-1:10=b.mp4 或 62-70=2（第 2 個 --take）。時間以完整原曲為準。"""
    res: list[Assign] = []
    for item in items:
        rng, sep, ref = item.rpartition("=")
        m = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", rng)
        if not sep or not m:
            die(f"--assign 格式錯誤：{item!r}（例：1:02-1:10=b.mp4 或 62-70=2）")
        a, b = parse_time(m.group(1)), parse_time(m.group(2))
        if b <= a:
            die(f"--assign 時間範圍錯誤：{item!r}")
        ref = ref.strip()
        idx = None
        if ref.isdigit():
            idx = int(ref) - 1
            if not 0 <= idx < len(take_paths):
                die(f"--assign {item!r}：只有 {len(take_paths)} 個 take（編號從 1 開始）")
        else:
            key = os.path.normcase(os.path.abspath(ref))
            hits = [i for i, p in enumerate(take_paths)
                    if os.path.normcase(os.path.abspath(p)) == key
                    or os.path.basename(p) == os.path.basename(ref)]
            if len(hits) != 1:
                die(f"--assign {item!r}：找不到唯一對應的 take（請用檔名或編號）")
            idx = hits[0]
        res.append(Assign(a, b, idx))
    res.sort(key=lambda x: x.song_from)
    for p, q in zip(res, res[1:]):
        if q.song_from < p.song_to:
            die(f"--assign 範圍重疊：{p.song_from:.2f}–{p.song_to:.2f}s 與 {q.song_from:.2f}–{q.song_to:.2f}s")
    return res


def plan_segments(takes: list[Take], beats: np.ndarray, start: float, end: float, fps: int,
                  every: int, min_seg: float, nudge: float,
                  assigns: list[Assign] | None = None, snap: float = 0.15) -> list[Segment]:
    assigns = assigns or []

    def snap_beat(t: float) -> float:
        if len(beats):
            j = int(np.argmin(np.abs(beats - t)))
            if abs(beats[j] - t) <= snap:
                return float(beats[j])
        return t

    # 指定區間的邊界一定是剪接點（離拍點 snap 秒內就吸到拍點上）
    forced_ranges = []
    for x in assigns:
        a, b = max(start, snap_beat(x.song_from)), min(end, snap_beat(x.song_to))
        if b - a <= 1.0 / fps:
            print(f"  注意：指定 {x.song_from:.2f}–{x.song_to:.2f}s 不在 --start/--end 範圍內，已忽略")
            continue
        forced_ranges.append((a, b, x.take))
    forced = sorted({start, end, *[v for a, b, _ in forced_ranges for v in (a, b)]})

    inside = [b for b in beats if start < b < end]
    auto = inside[every - 1::every]
    cuts = list(forced)
    for c in auto:  # 自動切點：不能太靠近任何既有切點
        if all(abs(c - f) >= min_seg for f in cuts):
            cuts.append(c)
    cuts.sort()

    def forced_take(a: float, b: float):
        mid = (a + b) / 2
        for fa, fb, idx in forced_ranges:
            if fa <= mid < fb:
                return idx
        return None

    raw: list[tuple[float, float, int, bool]] = []
    last = -1
    rr = 0
    for a, b in zip(cuts, cuts[1:]):
        covers = lambda i: takes[i].vfrom <= a - nudge and b - nudge <= takes[i].vto
        want = forced_take(a, b)
        if want is not None:
            if not covers(want):
                t = takes[want]
                die(f"指定 {Path(t.path).name} 用在原曲 {a:.2f}–{b:.2f}s，"
                    f"但它只拍到 {t.vfrom + nudge:.2f}–{t.vto + nudge:.2f}s")
            choice = want
        else:
            ok = [i for i in range(len(takes)) if covers(i)]
            if not ok:
                die(f"原曲 {a:.2f}–{b:.2f}s 沒有任何 take 有畫面；請補拍或調整 --start/--end")
            # 自動段避開「前一段」和「下一段指定」用的 take，避免看起來沒切
            nxt = forced_take(b, b + 1e-3) if b < end else None
            pool = [i for i in ok if i not in (last, nxt)] or [i for i in ok if i != last] or ok
            choice = pool[rr % len(pool)]
            rr += 1
        if raw and raw[-1][2] == choice and abs(raw[-1][1] - a) < 1e-9:
            raw[-1] = (raw[-1][0], b, choice, raw[-1][3] or want is not None)  # 同一 take 連續 → 合併
        else:
            raw.append((a, b, choice, want is not None))
        last = choice

    segs: list[Segment] = []
    for a, b, choice, assigned in raw:
        g0, g1 = round((a - start) * fps), round((b - start) * fps)
        if g1 <= g0:
            continue
        t = takes[choice]
        seek = (start + g0 / fps - nudge) - t.offset + t.corr
        segs.append(Segment(a, b, choice, max(0.0, seek), g1 - g0, assigned))
    return segs


# ───────────────────────────── 4. 歌詞字幕

def parse_lrc(path: str) -> list[tuple[float, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    off = 0.0
    m = re.search(r"\[offset:\s*([+-]?\d+)\s*\]", text, re.I)
    if m:
        off = int(m.group(1)) / 1000.0
    rows = []
    tag = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    for line in text.splitlines():
        times = tag.findall(line)
        lyric = tag.sub("", line).strip()
        for mm, ss in times:
            rows.append((int(mm) * 60 + float(ss) - off, lyric))
    rows.sort()
    return rows


def wrap_line(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    mid = len(s) // 2
    best = None
    for i, ch in enumerate(s):
        if ch in " ，,、。！!？?":
            if best is None or abs(i - mid) < abs(best - mid):
                best = i
    cut = best + 1 if best is not None and abs(best - mid) <= len(s) // 3 else mid
    return s[:cut].strip() + r"\N" + s[cut:].strip()


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(rows: list[tuple[float, str]], start: float, dur: float, out: Path,
              font: str, size: int, limit: int, lead: float) -> int:
    events = []
    for i, (t, lyric) in enumerate(rows):
        if not lyric:
            continue
        nxt = rows[i + 1][0] if i + 1 < len(rows) else t + 5.0
        a, b = t - start - lead, min(nxt, t + 6.0) - start - 0.05
        if b <= 0 or a >= dur:
            continue
        a, b = max(0.0, a), min(dur - 0.05, b)
        if b - a < 0.3:
            continue
        safe = lyric.replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Lyric,,0,0,0,,{wrap_line(safe, limit)}")
    out.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Lyric,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,80,80,%d,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n%s\n"
        % (W, H, font, size, int(H * 0.25), "\n".join(events)), encoding="utf-8")
    return len(events)


# ───────────────────────────── 5. 出片

def render(takes: list[Take], segs: list[Segment], song: str, start: float, dur: float, fps: int,
           ass: Path | None, out: str, work: Path, crf: int) -> None:
    vf = f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    lst = []
    for n, s in enumerate(segs):
        p = work / f"seg{n:03d}.mp4"
        print(f"  片段 {n + 1}/{len(segs)}：原曲 {s.song_from:6.2f}–{s.song_to:6.2f}s  ← {Path(takes[s.take].path).name}{'（指定）' if s.assigned else ''}")
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s.seek:.4f}", "-i", takes[s.take].path,
             "-an", "-vf", vf, "-frames:v", str(s.frames), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "12", "-pix_fmt", "yuv420p", "-r", str(fps), str(p)])
        lst.append(f"file '{p.as_posix()}'")
    (work / "list.txt").write_text("\n".join(lst) + "\n", encoding="utf-8")
    total = sum(s.frames for s in segs) / fps
    fade = min(0.4, total / 4)
    vfin = "null"
    if ass is not None:
        esc = ass.as_posix().replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vfin = f"ass='{esc}'"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
         "-ss", f"{start:.4f}", "-t", f"{total:.4f}", "-i", song,
         "-map", "0:v", "-map", "1:a", "-vf", vfin,
         "-af", f"afade=t=in:d=0.03,afade=t=out:st={total - fade:.3f}:d={fade:.3f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", "-t", f"{total:.4f}", out])


# ───────────────────────────── 主程式

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="對嘴 Shorts：自動對齊 take、拍點切換、歌詞字幕、1080x1920 出片")
    ap.add_argument("--song", required=True, help="原曲音檔（唯一音軌）")
    ap.add_argument("--take", action="append", required=True, help="拍攝片段，可重複；需有相機收音")
    ap.add_argument("--start", type=float, default=0.0, help="使用原曲的起點秒數")
    ap.add_argument("--end", type=float, help="使用原曲的終點秒數（預設 start+20）")
    ap.add_argument("--lrc", help="歌詞 .lrc（時間以完整原曲為準）")
    ap.add_argument("--out", default="short.mp4")
    ap.add_argument("--beats-per-cut", type=int, default=4, help="每幾拍換一次 take（預設 4）")
    ap.add_argument("--min-seg", type=float, default=0.8, help="最短片段秒數")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--assign", action="append", default=[], metavar="FROM-TO=TAKE",
                    help="指定原曲某段用哪個 take，可重複；例：1:02-1:10=b.mp4 或 62-70=2（第 2 個 --take）")
    ap.add_argument("--snap", type=float, default=0.15, help="--assign 邊界離拍點多近就吸到拍點（秒）")
    ap.add_argument("--offset", action="append", default=[], metavar="TAKE=SEC", help="手動指定偏移")
    ap.add_argument("--nudge-ms", type=float, default=0.0, help="整體嘴型微調；正值＝畫面往後延")
    ap.add_argument("--font", default="Noto Sans CJK TC", help="字幕字型（系統內字型名稱）")
    ap.add_argument("--font-size", type=int, default=78)
    ap.add_argument("--line-chars", type=int, default=12, help="單行最多字數，超過自動折兩行")
    ap.add_argument("--lyric-lead", type=float, default=0.1, help="字幕比唱早出現的秒數")
    ap.add_argument("--min-confidence", type=float, default=1.15,
                    help="對齊信心（最佳÷次佳峰值）低於此值就停止，預設 1.15")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--plan-only", action="store_true", help="只產生 plan.json，不出片")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"找不到 {tool}，請先安裝 ffmpeg")
    for p in [args.song, *args.take, *( [args.lrc] if args.lrc else [] )]:
        if not os.path.isfile(p):
            die(f"找不到檔案：{p}")
    end = args.end if args.end is not None else args.start + 20.0
    song_dur = probe(args.song)["duration"]
    end = min(end, song_dur)
    if end - args.start < 1.0:
        die("--start/--end 範圍太短")
    dur = end - args.start
    if dur > 60:
        print(f"注意：片長 {dur:.1f}s 超過 60 秒；含版權音樂的長 Shorts 可能被限制或封鎖")

    manual = {}
    for item in args.offset:
        k, _, v = item.partition("=")
        manual[os.path.normcase(os.path.abspath(k))] = float(v)

    assigns = parse_assigns(args.assign, args.take)  # 先檢查格式，避免跑完對齊才報錯

    print("[1/4] 讀取原曲並計算拍點…")
    song_wave = load_audio(args.song, ALIGN_SR)
    song_feat = band_features(song_wave)
    bpm, beats = detect_beats(load_audio(args.song, BEAT_SR))
    print(f"  BPM ≈ {bpm:.1f}，拍點 {len(beats)} 個")

    print("[2/4] 對齊每個 take…")
    takes: list[Take] = []
    failed = False
    for path in args.take:
        info = probe(path)
        if info["vstart"] is None:
            die(f"{path} 沒有影像串流")
        key = os.path.normcase(os.path.abspath(path))
        if key in manual:
            off, conf, is_manual = manual[key], float("nan"), True
        else:
            off, conf = align_take(song_feat, song_wave, path)
            is_manual = False
        astart = info["astart"] if info["astart"] is not None else info["fstart"]
        corr = astart - info["fstart"]
        v0 = info["vstart"] - info["fstart"]
        vfrom = v0 + off - corr
        vto = vfrom + (info["vdur"] or 0) - 1.0 / args.fps
        takes.append(Take(path, off, conf, is_manual, corr, vfrom, vto))
        flag = "手動" if is_manual else f"信心 {conf:4.2f}"
        print(f"  {Path(path).name}: offset {off:+.3f}s（{flag}），涵蓋原曲 {vfrom:.2f}–{vto:.2f}s")
        if not is_manual and conf < args.min_confidence:
            print(f"  ⚠ {Path(path).name} 對齊信心偏低：請確認相機有收到原曲，或用 --offset 手動指定")
            failed = True
    if failed:
        die(f"有 take 對齊信心低於 {args.min_confidence}（可用 --min-confidence 降低門檻，但請先人工確認）")

    print("[3/4] 依拍點規劃剪輯…")
    segs = plan_segments(takes, beats, args.start, end, args.fps, max(1, args.beats_per_cut),
                         args.min_seg, args.nudge_ms / 1000.0, assigns, args.snap)
    out = Path(args.out)
    plan_path = out.with_suffix(".plan.json")
    plan = {"song": args.song, "start": args.start, "end": end, "fps": args.fps, "bpm": round(bpm, 2),
            "nudge_ms": args.nudge_ms, "takes": [asdict(t) for t in takes],
            "assigns": [asdict(x) for x in assigns],
            "segments": [asdict(s) for s in segs],
            "beats_in_range": [round(float(b), 3) for b in beats if args.start <= b <= end]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {len(segs)} 個片段，計畫已寫入 {plan_path}")
    if args.plan_only:
        return 0

    print("[4/4] 出片…")
    work = Path(tempfile.mkdtemp(prefix="lipsync_"))
    try:
        ass = None
        if args.lrc:
            ass = work / "lyrics.ass"
            n = write_ass(parse_lrc(args.lrc), args.start, dur, ass, args.font, args.font_size,
                          args.line_chars, args.lyric_lead)
            print(f"  歌詞字幕 {n} 行")
        render(takes, segs, args.song, args.start, dur, args.fps, ass, str(out), work, args.crf)
    finally:
        if args.keep_temp:
            print(f"  暫存檔保留在 {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"完成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
