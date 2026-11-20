"""站点服务（两个维修站各部署一个实例）。

数据来源：固定的月度 CSV 文件，每行 = 组合编号 + 同单更换次数，
同月同组合只有一行。服务对外只提供两种受控取数方式：
  1. GET /api/rank?month=YYYY-MM&page=N  按次数从高到低，每页 5 条
  2. GET /api/count?month=YYYY-MM&combo=编号  按编号查次数，缺号计 0
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

from flask import Flask, jsonify, request

PAGE_SIZE = 5
DATA_DIR = Path(os.environ.get("STATION_DATA_DIR", "/data"))
STATION_NAME = os.environ.get("STATION_NAME", "station")


def _load_month(month: str) -> list[dict]:
    """读取某月 CSV 并按 (次数降序, 编号字典序) 排好序。"""
    path = DATA_DIR / f"{month}.csv"
    if not path.is_file():
        raise FileNotFoundError(month)
    rows: list[dict] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "combo_id" not in reader.fieldnames or "count" not in reader.fieldnames:
            raise ValueError("CSV 表头必须包含 combo_id,count")
        for line_no, row in enumerate(reader, start=2):
            combo = (row.get("combo_id") or "").strip()
            raw_count = (row.get("count") or "").strip()
            if not combo:
                raise ValueError(f"第 {line_no} 行组合编号为空")
            if combo in seen:
                raise ValueError(f"组合 {combo} 在同月数据中重复出现")
            seen.add(combo)
            try:
                count = int(raw_count)
            except ValueError as exc:
                raise ValueError(f"第 {line_no} 行次数不是整数: {raw_count!r}") from exc
            if count < 0:
                raise ValueError(f"第 {line_no} 行次数为负数")
            rows.append({"combo_id": combo, "count": count})
    # 中心只能按次数从高到低取；同分时按编号字典序，保证次序确定
    rows.sort(key=lambda r: (-r["count"], r["combo_id"]))
    return rows


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["STATION_NAME"] = STATION_NAME

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "station": STATION_NAME})

    @app.get("/api/rank")
    def rank():
        month = (request.args.get("month") or "").strip()
        page_raw = (request.args.get("page") or "1").strip()
        if not month:
            return jsonify({"error": "缺少 month 参数"}), 400
        try:
            page = int(page_raw)
            if page < 1:
                raise ValueError
        except ValueError:
            return jsonify({"error": "page 必须是 >=1 的整数"}), 400
        try:
            rows = _load_month(month)
        except FileNotFoundError:
            return jsonify({"error": f"站点 {STATION_NAME} 没有 {month} 的数据"}), 404
        except ValueError as exc:
            return jsonify({"error": f"站点 {STATION_NAME} 数据文件异常: {exc}"}), 500

        total = len(rows)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 0
        start = (page - 1) * PAGE_SIZE
        page_rows = rows[start:start + PAGE_SIZE]
        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_next": start + PAGE_SIZE < total,
            "items": page_rows,
        })

    @app.get("/api/count")
    def count():
        month = (request.args.get("month") or "").strip()
        combo = (request.args.get("combo") or "").strip()
        if not month or not combo:
            return jsonify({"error": "缺少 month 或 combo 参数"}), 400
        try:
            rows = _load_month(month)
        except FileNotFoundError:
            return jsonify({"error": f"站点 {STATION_NAME} 没有 {month} 的数据"}), 404
        except ValueError as exc:
            return jsonify({"error": f"站点 {STATION_NAME} 数据文件异常: {exc}"}), 500
        value = 0  # 缺号计零
        for row in rows:
            if row["combo_id"] == combo:
                value = row["count"]
                break
        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "combo_id": combo,
            "count": value,
        })

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
