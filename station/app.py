"""站点服务（两个维修站各部署一个实例）。

数据来源：固定的月度 CSV 文件。
  * 计数文件：YYYY-MM.csv，每行 = 组合编号 + 同单更换次数；
  * 零件字典：YYYY-MM.parts.csv，每行 = 组合编号 + 完整零件号列表
    （分号分隔，比较时按集合、顺序不限）。

服务对外提供两组受控取数方式：
  1. 组合分析接口（原有）：
     GET /api/rank?month=YYYY-MM&page=N   按次数降序，每页 5 条
     GET /api/count?month=YYYY-MM&combo=编号  按编号点查，缺号计 0
  2. 月报核对接口（新增）：
     GET /api/report/meta?month=YYYY-MM
         返回当月快照版号、记录数、总页数；
     GET /api/report?month=YYYY-MM&version=版号&page=N
         按编号分页返回条目（编号、次数、零件集合摘要），必须带同一版号；
     GET /api/report/parts?month=YYYY-MM&version=版号&combo=编号
         返回某编号的完整零件清单，必须带同一版号。

版号由当月计数文件与零件字典的全部内容共同计算而来：内容固定则版号
固定；任一处内容变化即产生新版号，旧版号不可再读（返回 409）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

PAGE_SIZE = 5
VERSION_LEN = 16
DIGEST_LEN = 16
DATA_DIR = Path(os.environ.get("STATION_DATA_DIR", "/data"))
STATION_NAME = os.environ.get("STATION_NAME", "station")


def _load_counts(month: str) -> list[dict]:
    """读取某月计数 CSV 并按 (次数降序, 编号字典序) 排好序。"""
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


def _load_parts(month: str) -> dict[str, list[str]]:
    """读取某月零件字典。

    文件不存在表示当月没有配套字典（所有记录都按缺定义处理）；
    文件存在但格式异常则视为数据错误。
    """
    path = DATA_DIR / f"{month}.parts.csv"
    if not path.is_file():
        return {}
    parts: dict[str, list[str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "combo_id" not in reader.fieldnames or "parts" not in reader.fieldnames:
            raise ValueError("零件字典表头必须包含 combo_id,parts")
        for line_no, row in enumerate(reader, start=2):
            combo = (row.get("combo_id") or "").strip()
            raw_parts = (row.get("parts") or "").strip()
            if not combo:
                raise ValueError(f"零件字典第 {line_no} 行组合编号为空")
            if combo in parts:
                raise ValueError(f"零件字典中组合 {combo} 重复出现")
            if not raw_parts:
                # 空定义等同于缺定义，保留该键但零件集合为空
                parts[combo] = []
                continue
            values = [p.strip() for p in raw_parts.split(";")]
            if any(not p for p in values):
                raise ValueError(f"零件字典第 {line_no} 行含空零件号")
            # 零件组按集合比较，顺序不限；统一去重并排序
            parts[combo] = sorted(set(values))
    return parts


def _parts_digest(parts: list[str]) -> str:
    """零件集合摘要：对排序去重后的零件号做 SHA-256，与顺序无关。"""
    canonical = "\x1f".join(sorted(set(parts)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def _load_snapshot(month: str) -> tuple[str, list[dict], dict[str, int], dict[str, list[str]]]:
    """读取当月计数与字典，返回 (版号, 上报条目, 编号->次数, 编号->零件)。

    版号只由两份文件的规范化内容决定：同版内容固定，改任一内容即换版。
    返回值里的零件字典与版号来自同一次读取，避免同一请求混用两个版本。
    """
    counts = _load_counts(month)
    parts = _load_parts(month)
    counts_by_id = {row["combo_id"]: row["count"] for row in counts}

    canonical = {
        "counts": sorted(([cid, cnt] for cid, cnt in counts_by_id.items())),
        "parts": sorted(([cid, ps] for cid, ps in parts.items())),
    }
    version = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:VERSION_LEN]

    items: list[dict] = []
    for combo in sorted(counts_by_id):
        defined_parts = parts.get(combo)
        defined = bool(defined_parts)  # 缺失或空定义都算缺定义
        items.append({
            "combo_id": combo,
            "count": counts_by_id[combo],
            "parts_defined": defined,
            "parts_digest": _parts_digest(defined_parts) if defined else None,
            "parts_count": len(defined_parts) if defined else 0,
        })
    return version, items, counts_by_id, parts


def _month_or_error(month: str):
    """加载月份快照；失败时返回可直接返回给客户端的错误响应。"""
    try:
        return _load_snapshot(month), None
    except FileNotFoundError:
        return None, (
            jsonify({"error": f"站点 {STATION_NAME} 没有 {month} 的数据",
                     "code": "month_not_found"}),
            404,
        )
    except ValueError as exc:
        return None, (
            jsonify({"error": f"站点 {STATION_NAME} 数据文件异常: {exc}",
                     "code": "bad_data"}),
            500,
        )


def _version_guard(month: str, requested: str):
    """校验请求携带的版号仍是当前版；不是则返回 409（旧版已撤下）。

    返回 (snapshot, error_response)；snapshot 为 None 表示应直接返回错误。
    """
    loaded, error = _month_or_error(month)
    if error is not None:
        return None, error
    snapshot_version, items, _, _ = loaded
    if not requested:
        return None, (
            jsonify({"error": "缺少 version 参数（必须与首次取数时的版号一致）",
                     "code": "version_required"}),
            400,
        )
    if requested != snapshot_version:
        return None, (
            jsonify({
                "error": f"站点 {STATION_NAME} 已无版号 {requested}（当前版号 {snapshot_version}）",
                "code": "version_withdrawn",
                "version": snapshot_version,
            }),
            409,
        )
    return loaded, None


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
            rows = _load_counts(month)
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
            rows = _load_counts(month)
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

    @app.get("/api/report/meta")
    def report_meta():
        month = (request.args.get("month") or "").strip()
        if not month:
            return jsonify({"error": "缺少 month 参数"}), 400
        loaded, error = _month_or_error(month)
        if error is not None:
            return error
        version, items, _, _ = loaded
        total = len(items)
        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "version": version,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 0,
        })

    @app.get("/api/report")
    @app.get("/api/report/page")
    def report_page():
        month = (request.args.get("month") or "").strip()
        version = (request.args.get("version") or "").strip()
        page_raw = (request.args.get("page") or "1").strip()
        if not month:
            return jsonify({"error": "缺少 month 参数"}), 400
        try:
            page = int(page_raw)
            if page < 1:
                raise ValueError
        except ValueError:
            return jsonify({"error": "page 必须是 >=1 的整数"}), 400

        loaded, error = _version_guard(month, version)
        if error is not None:
            return error
        snapshot_version, items, _, _ = loaded
        total = len(items)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 0
        start = (page - 1) * PAGE_SIZE
        page_items = items[start:start + PAGE_SIZE]
        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "version": snapshot_version,
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_next": start + PAGE_SIZE < total,
            "items": page_items,
        })

    @app.get("/api/report/parts")
    def report_parts():
        month = (request.args.get("month") or "").strip()
        version = (request.args.get("version") or "").strip()
        combo = (request.args.get("combo") or "").strip()
        if not month or not combo:
            return jsonify({"error": "缺少 month 或 combo 参数"}), 400

        loaded, error = _version_guard(month, version)
        if error is not None:
            return error
        snapshot_version, _, counts_by_id, parts_by_id = loaded
        if combo not in counts_by_id:
            return jsonify({
                "error": f"站点 {STATION_NAME} 在 {month} 没有编号 {combo}",
                "code": "combo_not_found",
            }), 404

        # 直接使用与版号同一次读取的字典，保证返回的是同一版的完整清单
        parts = parts_by_id.get(combo) or []
        if not parts:
            return jsonify({
                "error": f"站点 {STATION_NAME} 的编号 {combo} 缺少零件定义",
                "code": "missing_definition",
            }), 404

        return jsonify({
            "station": STATION_NAME,
            "month": month,
            "version": snapshot_version,
            "combo_id": combo,
            "count": counts_by_id[combo],
            "parts": parts,
            "parts_digest": _parts_digest(parts),
        })

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
