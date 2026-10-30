#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话轮换：软阈值 agent 决定，硬阈值系统兜底，关键会话永不动。

三个阈值管三件不同的事（详见 README.md）：
  1.0 MB  响应线 —— 只提醒，谁都不动
  2.5 MB  软阈值 —— agent 自己决定要不要轮换
  4.0 MB  硬阈值 —— 系统兜底：标记 + 轮换 + 告知

用法：
  python rotate.py --sessions-dir /path/to/sessions
  python rotate.py --sessions-dir /path/to/sessions --apply
  python rotate.py --sessions-dir /path/to/sessions --protected "master-,admin-"
"""
import argparse
import datetime
import io
import json
import os
import shutil
import sys

SESSION_FILE = "session.v4.jsonl.zstd"
TAIL_LINES = 60


def log(msg, logfile=None):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print("  " + msg)
    if logfile:
        try:
            with io.open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def find_sessions(root):
    """递归找工作区下的会话文件，返回 [{chat, dir, file, mb, mtime}]"""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        if SESSION_FILE not in filenames:
            continue
        f = os.path.join(dirpath, SESSION_FILE)
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        out.append({
            "chat": os.path.basename(dirpath),
            "dir": dirpath,
            "file": f,
            "mb": sz / 1048576.0,
            "mtime": os.path.getmtime(f),
        })
    return out


def read_tail(path, limit=TAIL_LINES):
    """读会话最后 N 条对话（zstd 解压，逐帧）。

    返回 [(时间, 谁, 文本)]。解不开就返回空列表——归档失败不应该阻塞轮换。
    """
    try:
        import zlib
        with open(path, "rb") as f:
            buf = f.read()
        magic = b"\x28\xb5\x2f\xfd"
        starts = []
        i = 0
        while True:
            j = buf.find(magic, i)
            if j < 0:
                break
            starts.append(j)
            i = j + 1
        raw = b""
        for k, a in enumerate(starts):
            b = starts[k + 1] if k + 1 < len(starts) else len(buf)
            try:
                raw += zlib.decompress(buf[a:b], zlib.MAX_WBITS | 32)
            except Exception:
                try:
                    raw += zlib.decompressobj().decompress(buf[a:b])
                except Exception:
                    pass
    except Exception:
        return []

    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
        if t not in ("user/message", "assistant/message"):
            continue
        ts = r.get("time", 0)
        try:
            dt = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
        except Exception:
            dt = "?"
        d = r.get("data") or {}
        txt = ""
        for key in ("text", "content", "message"):
            v = d.get(key)
            if isinstance(v, str):
                txt = v
                break
            if isinstance(v, list):
                parts = []
                for blk in v:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        parts.append(str(blk.get("text") or ""))
                txt = "".join(parts)
                break
        if not txt:
            continue
        rows.append((dt, "agent" if t == "assistant/message" else "user", txt.strip()))
    return rows[-limit:]


def archive_tail(chat_dir, chat, rows, mb, archive_root):
    os.makedirs(archive_root, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(archive_root, "%s-%s.md" % (chat.replace(os.sep, "_"), stamp))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("# 会话轮换存档：%s\n\n" % chat)
        f.write("> 上下文已到 %.2f MB，按阈值轮换。下面是轮换前最后 %d 条对话（原文），\n" % (mb, len(rows)))
        f.write("> 以后想回忆这段时间，搜这个文件。\n\n")
        for dt, who, txt in rows:
            f.write("- `%s` **%s**：%s\n" % (dt, who, txt[:300]))
    return path


def main():
    ap = argparse.ArgumentParser(description="上下文会话轮换")
    ap.add_argument("--sessions-dir", required=True, help="会话文件根目录")
    ap.add_argument("--limit-mb", type=float, default=2.5, help="软阈值（agent 决定）")
    ap.add_argument("--warn-mb", type=float, default=1.0, help="响应线（只提醒）")
    ap.add_argument("--hard-factor", type=float, default=1.6, help="硬阈值 = limit × factor")
    ap.add_argument("--protected", default="", help="永不动的会话（目录名前缀，逗号分隔）")
    ap.add_argument("--archive-dir", default="", help="归档目录（默认同级 _archive）")
    ap.add_argument("--state-file", default="", help="状态文件（默认 <sessions-dir>/_state/context-warning.json）")
    ap.add_argument("--apply", action="store_true", help="不加只预演")
    a = ap.parse_args()

    root = os.path.abspath(a.sessions_dir)
    prot = tuple(p.strip() for p in a.protected.split(",") if p.strip())
    archive_root = a.archive_dir or os.path.join(os.path.dirname(root), "_archive")
    state_dir = os.path.join(root, "_state")
    state_file = a.state_file or os.path.join(state_dir, "context-warning.json")
    logfile = os.path.join(state_dir, "rotate.log")
    os.makedirs(state_dir, exist_ok=True)

    ss = find_sessions(root)
    ss.sort(key=lambda r: r["mb"], reverse=True)
    if not ss:
        log("没有找到会话文件（%s）" % root, logfile)
        return 0

    # ── 1) 响应线：只提醒 ──────────────────────────────
    slow = [s for s in ss if s["mb"] >= a.warn_mb]
    if slow:
        for s in slow:
            log("响应提醒（%.2f MB ≥ %.2f，建议开新会话）：%s" % (s["mb"], a.warn_mb, s["chat"]), logfile)

    # ── 2) 软阈值 / 硬阈值 ────────────────────────────
    hard = a.limit_mb * a.hard_factor
    auto = []
    for s in ss:
        if s["mb"] < a.limit_mb:
            continue
        if prot and s["chat"].startswith(prot):
            log("保护（不动）：%s 已 %.2f MB" % (s["chat"], s["mb"]), logfile)
            continue
        if s["mb"] >= hard:
            auto.append(s)
            log("兜底（%.2f MB ≥ 硬阈值 %.2f，系统标记）：%s" % (s["mb"], hard, s["chat"]), logfile)
        else:
            log("告警（未标记，不动）：%s 已 %.2f MB" % (s["chat"], s["mb"]), logfile)

    # ── 3) 写状态文件给 agent 看 ──────────────────────
    state = {}
    try:
        state = json.load(io.open(state_file, encoding="utf-8"))
    except Exception:
        state = {}
    if slow:
        state["slow_sessions"] = {
            "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "chats": [{"chat": s["chat"], "mb": round(s["mb"], 2)} for s in slow],
            "why": "会话超过 %.1f MB，回话会变慢甚至卡死。建议开新会话。" % a.warn_mb,
        }
    if auto:
        state["auto_rotated"] = {
            "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "chats": [s["chat"] for s in auto],
            "why": "超过 %.1f MB 硬阈值，系统替你轮换了（已归档）" % hard,
        }
    if a.apply:
        try:
            io.open(state_file, "w", encoding="utf-8").write(json.dumps(state, ensure_ascii=False, indent=1))
        except Exception as e:
            log("写状态文件失败：%r" % (e,), logfile)

    print()
    print("  共 %d 个会话；响应线 %d 个，硬阈值兜底 %d 个" % (len(ss), len(slow), len(auto)))
    for s in ss[:10]:
        tag = ""
        if prot and s["chat"].startswith(prot):
            tag = "  ← 保护"
        elif s in auto:
            tag = "  ← 兜底"
        print("    %-40s %6.2f MB%s" % (s["chat"], s["mb"], tag))

    if not auto:
        return 0
    if not a.apply:
        print("\n  （预演模式，未做任何改动；加 --apply 才执行）")
        return 0

    # ── 4) 真轮换：归档 → 挪走 ──────────────────────
    day = datetime.datetime.now().strftime("%Y%m%d")
    for s in auto:
        rows = read_tail(s["file"])
        if rows:
            p = archive_tail(s["dir"], s["chat"], rows, s["mb"], os.path.join(archive_root, day))
            log("已归档 %d 条 → %s" % (len(rows), os.path.basename(p)), logfile)
        dst = os.path.join(archive_root, day, "raw", s["chat"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(s["dir"], dst)
            log("已轮换：%s → %s" % (s["chat"], dst), logfile)
        except Exception as e:
            log("轮换失败 %s：%r" % (s["chat"], e), logfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
