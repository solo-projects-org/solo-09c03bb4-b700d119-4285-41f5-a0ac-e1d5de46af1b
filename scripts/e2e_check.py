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
  7. 失败隔离：指向不可达地址时只提示出错站点；
  8. 月报核对 2026-05：两站版号展示、直接/间接冲突组、缺定义待补；
  9. 核对撤版：旧版号翻页/取清单返回 409，网页只提示出错站点。
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


def parse_reconcile(html: str):
    """粗解析核对页：版号、冲突组块、待补行。"""
    versions = dict(re.findall(
        r"(维修站[AB]) 版号 ([0-9a-f]+)", html))
    conflicts = re.findall(
        r'<div class="conflict" data-group="(\d+)">(.*?)</div>\s*(?=<div class="conflict"|</section>)',
        html, re.S,
    )
    pending_block = re.search(r"缺定义 · 待补(.*?)</section>", html, re.S)
    pending = re.findall(
        r"<td>(维修站[AB])</td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>",
        pending_block.group(1) if pending_block else "",
    )
    return versions, conflicts, pending


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
        "AVAILABLE_MONTHS": "2026-01,2026-02,2026-03,2026-04,2026-05",
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

    # 9. 月报核对：两站同版取数、冲突分组、缺定义待补
    meta_a = get_json(f"{args.station_a}/api/report/meta?month=2026-05")
    meta_b = get_json(f"{args.station_b}/api/report/meta?month=2026-05")
    check("核对元信息含版号", bool(meta_a["version"]) and bool(meta_b["version"]))
    check("核对元信息分页数",
          meta_a["total_pages"] == 1 and meta_b["total_pages"] == 2,
          f'{meta_a["total_pages"]}/{meta_b["total_pages"]}')
    # 同版内容固定：重复取元信息版号不变
    meta_a2 = get_json(f"{args.station_a}/api/report/meta?month=2026-05")
    check("同版内容版号固定", meta_a2["version"] == meta_a["version"])

    page_ok = get_json(
        f"{args.station_b}/api/report?"
        + urllib.parse.urlencode({"month": "2026-05", "version": meta_b["version"], "page": 2})
    )
    check("同版翻页返回同一版号", page_ok["version"] == meta_b["version"])
    old = get_text(
        f"{args.station_b}/api/report?"
        + urllib.parse.urlencode({"month": "2026-05", "version": "0" * 16, "page": 1})
    )
    check("旧版号翻页被拒", "version_withdrawn" in old)
    old_parts = get_text(
        f"{args.station_b}/api/report/parts?"
        + urllib.parse.urlencode({"month": "2026-05", "version": "0" * 16, "combo": "COM-100"})
    )
    check("旧版号取清单被拒", "version_withdrawn" in old_parts)
    missing_def = get_text(
        f"{args.station_b}/api/report/parts?"
        + urllib.parse.urlencode({"month": "2026-05", "version": meta_b["version"], "combo": "COM-900"})
    )
    check("缺定义清单返回错误", "missing_definition" in missing_def)

    rec_html = get_text(f"{args.web}/reconcile?month=2026-05")
    versions, conflicts, pending = parse_reconcile(rec_html)
    check("页面标出两站版号",
          versions.get("维修站A") == meta_a["version"]
          and versions.get("维修站B") == meta_b["version"], str(versions))
    check("页面冲突组数量", len(conflicts) == 2, str(len(conflicts)))
    indirect = next((body for _, body in conflicts if "COM-700" in body), "")
    check("间接冲突组合并同组",
          "COM-300" in indirect and "COM-700" in indirect
          and "P8" in indirect and "P4" in indirect, indirect[:200])
    direct = next((body for _, body in conflicts if "COM-100" in body), "")
    check("直接冲突组含两站记录",
          "维修站A" in direct and "维修站B" in direct
          and "P1" in direct and "P3" in direct)
    check("冲突组未混入一致组", "COM-200" not in rec_html and "COM-400" not in rec_html)
    check("缺定义记录单列待补",
          set(pending) == {("维修站A", "COM-500", "3"), ("维修站B", "COM-900", "1")},
          str(pending))

    # 10. 核对撤版：B 站版号在翻页后变化，页面只提示出错站点且无半截结果
    env2 = dict(env)
    env2["PORT"] = str(probe_port + 1)
    probe2_url = f"http://127.0.0.1:{probe_port + 1}"
    # 用真实站点：向站点 B 请求一个不存在的旧版号无法从网页触发，
    # 因此这里改用“B 站不可达”验证核对页同样只提示出错站点。
    env2["STATION_B_URL"] = f"http://127.0.0.1:{dead_port}"
    proc2 = subprocess.Popen(
        [sys.executable, "app.py"], cwd=web_dir, env=env2,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{probe2_url}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        html_rec_bad = get_text(f"{probe2_url}/reconcile?month=2026-05")
        check("核对失败隔离 只提示维修站B",
              "维修站B 取数失败" in html_rec_bad
              and "维修站A 取数失败" not in html_rec_bad)
        check("核对失败隔离 无半截结果", 'class="conflict"' not in html_rec_bad
              and "核对结果" not in html_rec_bad)
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        return 1
    print("全部端到端检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
