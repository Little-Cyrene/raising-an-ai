#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一份 markdown 分镜做成视频：渐变背景 + 居中字幕 +（可选）TTS 旁白。

为什么是"渐变 + 字幕"而不是 AI 生成画面：
  本地小模型的生图一致性差，同一张脸在不同镜头里会漂，接起来像两个人。
  渐变背景不假装是画面——它就是容器。内容靠字和声音。

用法：
  # 纯文字（每镜固定 4 秒）
  python make.py --script story.md --out out.mp4

  # 带 TTS（要求这个命令能把 {text} 说成 {wav} 文件）
  python make.py --script story.md --out out.mp4 \
      --tts "python tts.py --text {text} --out {wav}"

分镜脚本格式（story.md）：
  # 标题
  why: 为什么做这条（可选，会写进文件名/元数据）
  ## 1
  图: gallery/a.png          ← 可选。不写就走渐变背景
  第一镜要说的话
  ## 2
  旁白: 这一镜的旁白可以跟画面文字不一样
  第二镜的画面文字

依赖：ffmpeg（要带 drawtext 和 gradients 滤镜，主流发行版都带）
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile

W, H = 1280, 720
FPS = 25

# 渐变配色（深色系，不刺眼）。两两一组，按镜头序号轮换。
GRADIENTS = [
    ("0x1a1a2e", "0x16213e"),
    ("0x221b3a", "0x1b2b4a"),
    ("0x2d1b2e", "0x1a2440"),
    ("0x1b2b2b", "0x20223a"),
    ("0x1e2235", "0x2a1f38"),
]

FONT_CANDIDATES = [
    "C\\:/Windows/Fonts/msyh.ttc",       # Windows 微软雅黑
    "/System/Library/Fonts/PingFang.ttc",  # macOS
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def ffmpeg_bin(name="ffmpeg"):
    return name


def pick_font():
    for f in FONT_CANDIDATES:
        real = f.replace("\\:", ":").replace("\\/", "/")
        if os.path.exists(real):
            return f
    return FONT_CANDIDATES[0]


# ── 分镜解析 ────────────────────────────────────────────

def parse_script(path):
    title, why, shots = "", "", []
    cur = None
    with io.open(path, encoding="utf-8") as f:
        for raw in f:
            st = raw.strip()
            if st.startswith("# ") and not title:
                title = st[2:].strip()
            elif st.lower().startswith("why:") or st.startswith("为什么:"):
                why = st.split(":", 1)[1].strip()
            elif st.startswith("## "):
                if cur:
                    shots.append(cur)
                cur = {"text": "", "image": "", "voice": ""}
            elif cur is not None:
                low = st.lower()
                if low.startswith("图:") or low.startswith("img:"):
                    cur["image"] = st.split(":", 1)[1].strip()
                elif low.startswith("旁白:") or low.startswith("voice:"):
                    cur["voice"] = st.split(":", 1)[1].strip()
                elif st:
                    cur["text"] = (cur["text"] + "\n" + st) if cur["text"] else st
    if cur:
        shots.append(cur)
    for s in shots:
        if not s["voice"]:
            s["voice"] = s["text"]
    return title, why, shots


# ── 字幕滤镜 ────────────────────────────────────────────

def subtitle_filter(text, size=42, per_line=15, max_lines=4):
    """切成几行，居中 + 描边。

    drawtext 的 text= 里这些字符要转义（否则 filtergraph 会炸）：
      :  → \:
      ,  → \,
      %  → \%
      \\ → \\\\
      '  → 换成 ’（单引号会破坏引号配对）
    """
    if not text or not text.strip():
        return ""
    lines, cur = [], ""
    for ch in text.strip():
        cur += ch
        if ch in "。！？；\n" or len(cur) >= per_line:
            if cur.strip():
                lines.append(cur.strip())
            cur = ""
    if cur.strip():
        lines.append(cur.strip())
    lines = lines[:max_lines]

    font = pick_font()
    n = len(lines)
    parts = []
    for i, ln in enumerate(lines):
        y = "(h-text_h)/2%+d" % int((i - (n - 1) / 2.0) * (size + 18))
        esc = (ln.replace("\\", "\\\\").replace(":", "\\:")
                 .replace("'", "’").replace("%", "\\%").replace(",", "\\,"))
        parts.append(
            "drawtext=fontfile='%s':text='%s':fontsize=%d:fontcolor=white:"
            "borderw=2:bordercolor=0x000000@0.55:x=(w-text_w)/2:y=%s"
            % (font, esc, size, y)
        )
    return ",".join(parts)


# ── 工具 ────────────────────────────────────────────────

def run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or b"").decode("utf-8", "replace")


def audio_duration(path):
    ok, _ = run([ffmpeg_bin("ffprobe"), "-v", "error",
                 "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=30)
    if not ok:
        return 0.0
    try:
        out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error",
                              "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, timeout=30)
        return float((out.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def make_tts(cmd_tpl, text, wav):
    """按模板跑 TTS。{text} 和 {wav} 会被替换。

    注意：如果目标环境的沙箱不允许子进程开管道，这里应该改成交互式写法
    （stdout/stderr 都丢 DEVNULL），不要 capture_output。
    """
    cmd = cmd_tpl.replace("{text}", text.replace('"', '\\"')).replace("{wav}", wav)
    try:
        if os.name == "nt":
            subprocess.run(cmd, shell=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(cmd, shell=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return os.path.exists(wav)


# ── 单镜合成 ────────────────────────────────────────────

def render_shot(i, shot, out_path, tmpdir, tts_tpl, default_secs, keep_img):
    img = shot["image"]
    if img and not os.path.isabs(img):
        img = os.path.abspath(img)
    if img and not os.path.exists(img):
        print("    [%d] 图不存在，改用渐变：%s" % (i, img))
        img = ""

    wav = ""
    dur = 0.0
    if tts_tpl and shot["voice"].strip():
        cand = os.path.join(tmpdir, "v%02d.wav" % i)
        print("    [%d] 生成旁白…" % i)
        if make_tts(tts_tpl, shot["voice"], cand):
            wav = cand
            dur = audio_duration(wav)
    if not wav:
        dur = default_secs
    else:
        dur = max(dur + 0.6, 2.5)   # 语音后留一口气

    if img:
        # 有图：图 + 旁白
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p" % (W, H, W, H))
        cmd = [ffmpeg_bin(), "-y", "-loop", "1", "-i", img]
        if wav:
            cmd += ["-i", wav]
        cmd += ["-c:v", "libx264", "-tune", "stillimage", "-r", str(FPS), "-crf", "20",
                "-vf", vf, "-t", "%.2f" % dur, "-pix_fmt", "yuv420p"]
        if wav:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
        cmd += [out_path]
    else:
        # 无图：渐变背景 + 字幕
        c0, c1 = GRADIENTS[i % len(GRADIENTS)]
        bg = ("gradients=s=%dx%d:c0=%s:c1=%s:x0=0:y0=0:x1=%d:y1=%d:d=%.2f"
              % (W, H, c0, c1, W, H, dur))
        cmd = [ffmpeg_bin(), "-y", "-f", "lavfi", "-i", bg]
        if wav:
            cmd += ["-i", wav]
        cmd += ["-c:v", "libx264", "-r", str(FPS), "-crf", "20",
                "-pix_fmt", "yuv420p", "-t", "%.2f" % dur]
        sub = subtitle_filter(shot["text"])
        if sub:
            cmd += ["-vf", sub]
        if wav:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
        cmd += [out_path]

    ok, err = run(cmd, timeout=300)
    if os.path.exists(out_path):
        print("    [%d] OK（%.1f 秒）" % (i, dur))
        return out_path
    print("    [%d] 失败：%s" % (i, err[-300:]))
    return None


def main():
    ap = argparse.ArgumentParser(description="marksown 分镜 → 渐变字幕视频")
    ap.add_argument("--script", required=True, help="分镜 md 文件")
    ap.add_argument("--out", required=True, help="输出 mp4")
    ap.add_argument("--tts", default="", help="TTS 命令模板，用 {text} 和 {wav}")
    ap.add_argument("--seconds", type=float, default=4.0, help="无语音时每镜时长")
    ap.add_argument("--keep-tmp", action="store_true", help="保留中间文件（排错用）")
    a = ap.parse_args()

    if not os.path.exists(a.script):
        print("脚本不存在：%s" % a.script)
        return 2
    title, why, shots = parse_script(a.script)
    if not shots:
        print("没有镜头。用 `## 1` `## 2` 分段。")
        return 2
    print("《%s》 %d 个镜头%s" % (title or "(无标题)", len(shots),
                                 ("，为什么做：" + why) if why else ""))

    tmpdir = tempfile.mkdtemp(prefix="vg_")
    segs = []
    for i, s in enumerate(shots, 1):
        p = os.path.join(tmpdir, "s%02d.mp4" % i)
        r = render_shot(i, s, p, tmpdir, a.tts, a.seconds, False)
        if r:
            segs.append(r)

    if not segs:
        print("没有成功的片段")
        return 1

    lst = os.path.join(tmpdir, "list.txt")
    with io.open(lst, "w", encoding="utf-8") as f:
        for s in segs:
            f.write("file '%s'\n" % s.replace("\\", "/"))

    ok, err = run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", lst,
                   "-c", "copy", a.out], timeout=300)
    if not os.path.exists(a.out):
        print("拼接失败：%s" % err[-300:])
        return 1

    size = os.path.getsize(a.out) / 1048576.0
    print("\n做好了：%s（%.1f MB，%d 段）" % (a.out, size, len(segs)))
    if not a.keep_tmp:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print("中间文件在：%s" % tmpdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
