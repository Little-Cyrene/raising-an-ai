#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受限提权通道 · agent 端。

它做的事：写请求文件 → 等高权限那边轮询执行 → 读结果文件。
高权限那一半在 executor.ps1，靠计划任务每 2 分钟看一次。

用法：
  python client.py status         看通道通不通（最快，是一次真实的往返）
  python client.py <action>       请求一个白名单动作
  python client.py list           看白名单里有什么（从 executor.ps1 里读）

为什么等 150 秒：
  轮询周期 120 秒，最坏情况刚错过一次 → 要等下一轮。
  等 90 秒会刚好错过（我们踩过这个坑）。
"""
import argparse
import io
import json
import os
import sys
import time
import uuid

# 通道目录：请求/结果/日志都放这儿
BASE = os.environ.get("ADMIN_CHANNEL_DIR") or os.path.dirname(os.path.abspath(__file__))
REQ = os.path.join(BASE, "admin-req.json")
RES = os.path.join(BASE, "admin-res.json")
LOG = os.path.join(BASE, "admin-op.log")

WAIT_SECONDS = int(os.environ.get("ADMIN_CHANNEL_WAIT", "150"))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print("  " + msg)
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_req(action, payload=None):
    rid = uuid.uuid4().hex[:8]
    body = {"id": rid, "action": action, "ts": time.time()}
    if payload:
        body["payload"] = payload
    tmp = REQ + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(body, ensure_ascii=False))
    os.replace(tmp, REQ)      # 原子替换：避免高权限那边读到半个文件
    return rid


def wait_res(rid, timeout=WAIT_SECONDS):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with io.open(RES, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("id") == rid:
                return d
        except Exception:
            pass
        time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser(description="受限提权通道 · agent 端")
    ap.add_argument("action", help="白名单动作名（或 status / list）")
    ap.add_argument("--wait", type=int, default=WAIT_SECONDS, help="最多等多少秒")
    a = ap.parse_args()

    if a.action == "list":
        p = os.path.join(BASE, "executor.ps1")
        if not os.path.exists(p):
            print("找不到 executor.ps1")
            return 2
        txt = io.open(p, encoding="utf-8", errors="replace").read()
        import re
        acts = sorted(set(re.findall(r'^\s*"([a-z][a-z0-9-]+)"\s*\{', txt, re.M)))
        print("白名单动作：")
        for x in acts:
            print("  · " + x)
        return 0

    rid = write_req(a.action)
    log("已写请求 id=%s action=%s" % (rid, a.action))
    log("等高权限那边轮询（最多 %d 秒）…" % a.wait)
    d = wait_res(rid, a.wait)
    if d is None:
        log("超时：%d 秒内没等到结果" % a.wait)
        log("检查：① 计划任务在不在跑  ② executor.ps1 的路径对不对")
        return 1
    ok = d.get("ok")
    log("结果 ok=%s" % ok)
    out = d.get("out") or ""
    if out:
        for line in str(out).splitlines():
            print("    " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
