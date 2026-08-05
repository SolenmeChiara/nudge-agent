#!/usr/bin/env python3
"""音轨探针 —— 把视频或音频的声音扒成可读的数字和曲线。

起因是 2026-08-04 验证两条 Gemini Omni 生成视频：模型描述自己的片子时，
说对的地方和瞎编的地方措辞一样斩钉截铁，语气里没有信息量。想知道声音
是不是真跟画面对应，只能自己去量。

量三个东西：
  RMS      响度包络，看什么时候有事发生
  质心     频谱重心，粗略代表声音的「高低」，升调降调看这条
  过零率   高频成分的廉价代理，跟质心互相印证

最有用的其实是 --flat：自动标出质心纹丝不动的区段。生成模型糊弄的时候
不会做成静音，会铺一层底噪，听感上「有声音」，但那段的质心是一条死线。

用法：
    python3 audio_probe.py video.mp4
    python3 audio_probe.py video.mp4 --peaks          # 数冲击峰，看节奏齐不齐
    python3 audio_probe.py video.mp4 --width 48       # 手机宽度
    python3 audio_probe.py clip.wav --start 7.5       # 只看后半段
"""

import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np


def to_wav(path, sr=22050):
    """任意媒体文件转单声道 wav，返回临时文件路径。已经是 wav 就原样返回。"""
    if path.lower().endswith(".wav"):
        return path, False
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
           "-ac", "1", "-ar", str(sr), out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg 失败：{r.stderr.strip()[:400]}")
    return out, True


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def features(x, sr, win_s):
    """按固定窗切片，返回 (时间, RMS, 频谱质心, 过零率) 四条等长数组。"""
    win = max(64, int(sr * win_s))
    t, rms, cen, zcr = [], [], [], []
    hann = np.hanning(win)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    for i in range(0, len(x) - win, win):
        s = x[i:i + win]
        t.append(i / sr)
        rms.append(np.sqrt((s ** 2).mean()))
        sp = np.abs(np.fft.rfft(s * hann))
        cen.append((sp * freqs).sum() / max(sp.sum(), 1e-12))
        zcr.append(((s[:-1] * s[1:]) < 0).sum() / len(s))
    return (np.array(t), np.array(rms), np.array(cen), np.array(zcr))


def plot(t, rms, cen, width, height=13):
    """ASCII 曲线：纵轴质心，符号浓淡表示响度。"""
    if len(cen) == 0:
        return
    # 重采样到目标宽度
    idx = np.linspace(0, len(cen) - 1, width).astype(int)
    c, r = cen[idx], rms[idx]
    lo, hi = np.percentile(c, 2), np.percentile(c, 98)
    if hi - lo < 1:
        hi = lo + 1
    loud = np.percentile(rms, 90)
    mid = np.percentile(rms, 60)

    grid = [[" "] * width for _ in range(height)]
    for j in range(width):
        row = int((c[j] - lo) / (hi - lo) * (height - 1))
        row = max(0, min(height - 1, row))
        grid[height - 1 - row][j] = "@" if r[j] > loud else ("*" if r[j] > mid else ".")

    for k, line in enumerate(grid):
        val = hi - (hi - lo) * k / (height - 1)
        print(f"{val:5.0f} |{''.join(line)}")
    print("      +" + "-" * width)

    t0 = t[0]
    span = t[-1] - t0 + (t[1] - t0 if len(t) > 1 else 1)
    axis = [" "] * width
    step = max(1, round(span / 6))
    sec = int(t0) - int(t0) % step
    while sec <= t0 + span:
        if sec >= t0:
            p = int((sec - t0) / span * width)
            label = str(sec)
            if p + len(label) <= width:
                for m, ch in enumerate(label):
                    axis[p + m] = ch
        sec += step
    print("       " + "".join(axis) + "  秒")
    print("       纵轴＝频谱质心 Hz　　. 弱　* 中　@ 强")


def find_flat(t, cen, win_n, tol, drift=0.05):
    """找质心几乎不动的连续区段。

    光看窗内标准差不够：一段稳定爬升的曲线，在足够短的窗里标准差也很小，
    会被误判成死区——而那正是「有事发生」的地方。所以再拟一条直线，窗内
    的总漂移超过均值的 drift 比例就不算平坦。
    """
    if len(cen) < win_n * 2:
        return []
    flags = np.zeros(len(cen), dtype=bool)
    xs = np.arange(win_n)
    for i in range(len(cen) - win_n):
        seg = cen[i:i + win_n]
        m = seg.mean()
        if m <= 0 or seg.std() / m >= tol:
            continue
        slope = np.polyfit(xs, seg, 1)[0]
        if abs(slope * win_n) / m < drift:
            flags[i:i + win_n] = True
    spans, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            spans.append((t[start], t[i], cen[start:i].mean()))
            start = None
    if start is not None:
        spans.append((t[start], t[-1], cen[start:].mean()))
    return [s for s in spans if s[1] - s[0] >= 0.5]


def find_peaks(seg, sr, t0=0.0, hop_s=0.01, k=2.0, min_gap=0.12):
    """能量局部极大值当作冲击峰，用来数节奏。seg 是已经截好的片段，t0 只用于标时间。"""
    w = max(32, int(sr * hop_s))
    e = np.array([np.sqrt((seg[i:i + w] ** 2).mean())
                  for i in range(0, len(seg) - w, w)])
    if len(e) < 3:
        return []
    thr = e.mean() + k * e.std()
    peaks = []
    for i in range(1, len(e) - 1):
        if e[i] > thr and e[i] >= e[i - 1] and e[i] > e[i + 1]:
            tt = t0 + i * hop_s
            if not peaks or tt - peaks[-1] > min_gap:
                peaks.append(tt)
    return peaks


def main():
    ap = argparse.ArgumentParser(description="扒音轨，量响度与音高的走向")
    ap.add_argument("path", help="视频或音频文件（mp4 / wav / 任何 ffmpeg 认识的）")
    ap.add_argument("--width", type=int, default=72, help="ASCII 图宽度，手机用 48")
    ap.add_argument("--win", type=float, default=0.05, help="分析窗长，秒")
    ap.add_argument("--start", type=float, default=0.0, help="从第几秒开始看")
    ap.add_argument("--end", type=float, default=None, help="看到第几秒")
    ap.add_argument("--peaks", action="store_true", help="检测冲击峰并算间隔")
    ap.add_argument("--flat", action="store_true", default=True, help="标出无演化区段")
    ap.add_argument("--no-flat", dest="flat", action="store_false")
    a = ap.parse_args()

    p = a.path
    if len(p) > 2 and p[1] == ":":  # D:/xxx → /mnt/d/xxx
        p = f"/mnt/{p[0].lower()}{p[2:]}"
    if not os.path.exists(p):
        sys.exit(f"找不到文件：{p}")

    wav, tmp = to_wav(p)
    try:
        x, sr = read_wav(wav)
    finally:
        if tmp:
            os.unlink(wav)

    dur = len(x) / sr
    lo = int(a.start * sr)
    hi = int(a.end * sr) if a.end else len(x)
    xs = x[lo:hi]

    print(f"\n{os.path.basename(p)}　时长 {dur:.2f}s　采样率 {sr}")
    if a.start or a.end:
        print(f"截取 {a.start:.2f}s – {(a.end or dur):.2f}s")
    print()

    t, rms, cen, zcr = features(xs, sr, a.win)
    t = t + a.start
    plot(t, rms, cen, a.width)

    print(f"\n质心 {cen.min():.0f} – {cen.max():.0f} Hz"
          f"　　响度 {rms.min():.4f} – {rms.max():.4f}"
          f"　　过零率 {zcr.min():.3f} – {zcr.max():.3f}")

    if a.flat:
        spans = find_flat(t, cen, max(3, int(0.6 / a.win)), 0.10)
        if spans:
            print("\n没有演化的区段（质心波动 <10%，八成是底噪垫场）：")
            for s, e, m in spans:
                print(f"  {s:5.2f} – {e:5.2f}s　({e - s:.2f}s)　质心稳在 {m:.0f} Hz")
        else:
            print("\n全片质心都在动，没找到死区。")

    if a.peaks:
        pk = find_peaks(xs, sr, a.start)
        print(f"\n冲击峰 {len(pk)} 个：" + "  ".join(f"{v:.2f}" for v in pk))
        if len(pk) > 1:
            d = np.diff(pk)
            print(f"  间隔：{', '.join(f'{v:.2f}' for v in d)}")
            print(f"  均值 {d.mean():.3f}s　标准差 {d.std():.3f}s"
                  f"　抖动 {d.std() / d.mean() * 100:.0f}%"
                  f"　→ 每秒 {1 / d.mean():.1f} 下")
            print("  （抖动低于 5% 反而假，真人拍手在 10–25% 之间）")
    print()


if __name__ == "__main__":
    main()
