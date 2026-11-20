#!/usr/bin/env python3
"""端到端自测：对已启动的两个站点服务 + 分析网页做 HTTP 级验证。

用法（本地直跑或容器内均可）：
  python3 scripts/e2e_check.py \
      --web http://127.0.0.1:8000 \
      --station-a http://127.0.0.1:8001 \
      --station-b http://127.0.0.1:8002

覆盖：
  1. 站点分页每页 5 条、按次数降序同分字典序、越界页为空；
  2. 站点点查缺号计零；
  3. 场景一 2026-01 K=3：榜首两站首屏均无，各翻 2 页后停读；
  4. 场景二 2026-02 K=3：首屏后即停（各 1 页）；
  5. 2026-04 K=1：未耗尽站点通过点查确认缺号零；
  6. 同分按编号字典序（2026-03）；
  7. 失败隔离：指向不可达地址时只提示出错站点。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8")


def parse_result_table(html: str):
    rows = re.findall(
        r"<tr>\s*<td>(\d+)</td>\s*<td>([^<]+)</td>\s*"
        r"<td>(\d+)</td>\s*<td>(\d+)</td>\s*<td><strong>(\d+)</strong></td>",
        html,
    )
    pages = re.search(r"实际翻页数.*?</span>", html, re.S)
    counts = re.search(r"点查次数.*?</span>", html, re.S)
    clean = lambda m: unescape(re.sub(r"<[^>]+>", "", m.group(0))) if m else ""
    return rows, clean(pages), clean(counts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://127.0.0.1:8000")
    ap.add_argument("--station-a", default="http://127.0.0.1:8001")
    ap.add_argument("--station-b", default="http://127.0.0.1:8002")
    args = ap.parse_args()
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    # 1. 站点分页
    p1 = get_json(f"{args.station_a}/api/rank?month=2026-01&page=1")
    check("站点A 每页5条", len(p1["items"]) == 5)
    counts = [i["count"] for i in p1["items"]]
    check("站点A 按次数降序", counts == sorted(counts, reverse=True), str(counts))
    p2 = get_json(f"{args.station_a}/api/rank?month=2026-01&page=2")
    check("站点A 第二页含榜首Z999", any(i["combo_id"] == "Z999" for i in p2["items"]))
    check("站点A 第二页 has_next=false", p2["has_next"] is False)
    p3 = get_json(f"{args.station_a}/api/rank?month=2026-01&page=3")
    check("站点A 越界页为空", p3["items"] == [])

    # 2. 缺号计零
    miss = get_json(f"{args.station_b}/api/count?month=2026-01&combo=NO-SUCH")
    check("站点B 缺号计零", miss["count"] == 0)

    # 3-6. 网页聚合
    def query(month: str, k: int):
        url = f"{args.web}/query?{urllib.parse.urlencode({'month': month, 'k': k})}"
        return parse_result_table(get_text(url))

    rows, pages, cq = query("2026-01", 3)
    check("场景一 名单次序",
          [r[1] for r in rows] == ["Z999", "A01", "A02"], str([r[1] for r in rows]))
    check("场景一 榜首两站次数", rows[0][2:5] == ("9", "9", "18"), str(rows[0]))
    check("场景一 各翻2页后停读", "维修站A 2 页" in pages and "维修站B 2 页" in pages, pages)

    rows, pages, cq = query("2026-02", 3)
    check("场景二 名单次序",
          [r[1] for r in rows] == ["C001", "C002", "C003"], str([r[1] for r in rows]))
    check("场景二 首屏后即停(各1页)",
          "维修站A 1 页" in pages and "维修站B 1 页" in pages, pages)

    rows, pages, cq = query("2026-04", 1)
    check("点查场景 榜首X01及缺号零",
          rows and rows[0][1] == "X01" and rows[0][3] == "0", str(rows))
    check("点查场景 B站点查1次", "维修站B 1 次" in cq, cq)
    check("点查场景 仅各翻1页",
          "维修站A 1 页" in pages and "维修站B 1 页" in pages, pages)

    rows, _, _ = query("2026-03", 5)
    check("同分字典序 T01<T02<T03",
          [r[1] for r in rows[:3]] == ["T01", "T02", "T03"], str([r[1] for r in rows]))

    # 7. 失败隔离：临时起一个把 B 站指向不可达端口的 web 实例，
    #    页面只能提示出错站点（维修站B），且不出现任何结果表格。
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    dead_port = free.getsockname()[1]
    free.close()
    probe_port = dead_port + 1
    web_dir = Path(__file__).resolve().parent.parent / "web"
    env = dict(os.environ)
    env.update({
        "AVAILABLE_MONTHS": "2026-01,2026-02,2026-03,2026-04",
        "STATION_A_URL": args.station_a,
        "STATION_B_URL": f"http://127.0.0.1:{dead_port}",
        "PORT": str(probe_port),
    })
    proc = subprocess.Popen(
        [sys.executable, "app.py"], cwd=web_dir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        probe_url = f"http://127.0.0.1:{probe_port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{probe_url}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        html_bad = get_text(f"{probe_url}/query?month=2026-02&k=3")
        check("失败隔离 只提示维修站B",
              "维修站B 取数失败" in html_bad and "维修站A 取数失败" not in html_bad)
        check("失败隔离 无半截结果表", "<tbody>" not in html_bad)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 8. 参数校验：非法 K 只显示错误、不渲染结果表
    bad = get_text(f"{args.web}/query?month=2026-02&k=0")
    check("非法K显示错误且无结果表", 'class="error"' in bad and "<tbody>" not in bad)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        return 1
    print("全部端到端检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
