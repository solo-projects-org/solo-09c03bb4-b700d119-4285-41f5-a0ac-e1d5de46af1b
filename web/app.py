"""中心分析网页（Flask）。

页面一：选月份、填 K，按两站次数之和列出前 K 个组合（同分按编号字典序），
同时展示每站次数和实际翻页数。

页面二（月报核对）：按月份拉取两站当期同版数据，找出“同编号不同零件组”
或“同零件组不同编号”的冲突组（间接相连的也归在一起），只对冲突组
索取完整零件清单；缺定义的记录单列待补。页面标出两站各自的版号。

取数严格通过站点的受控接口进行，任一站点失败时只提示出错站点，
用户可重新查询。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask import Flask, render_template, request

from aggregator import StationError, top_k
from reconcile import reconcile

MONTHS = [m.strip() for m in os.environ.get("AVAILABLE_MONTHS", "2026-01,2026-02,2026-03").split(",") if m.strip()]
STATION_A_URL = os.environ.get("STATION_A_URL", "http://station-a:8000")
STATION_B_URL = os.environ.get("STATION_B_URL", "http://station-b:8000")
STATION_A_LABEL = os.environ.get("STATION_A_LABEL", "维修站A")
STATION_B_LABEL = os.environ.get("STATION_B_LABEL", "维修站B")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "5"))


def _http_get_json(base_url: str, path: str, params: dict[str, Any]) -> dict:
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也算该站点取数失败；尽量带出站点自己的错误描述
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = f"HTTP {exc.code}"
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法连接站点: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("响应格式不正确")
    return payload


def make_fetchers(base_url: str):
    def rank_fetcher(month: str, page: int) -> dict:
        return _http_get_json(base_url, "/api/rank", {"month": month, "page": page})

    def count_fetcher(month: str, combo: str) -> dict:
        return _http_get_json(base_url, "/api/count", {"month": month, "combo": combo})

    return rank_fetcher, count_fetcher


def make_report_fetchers(base_url: str):
    """月报核对接口：元信息、同版分页、同版完整零件清单。"""

    def meta_fetcher(month: str) -> dict:
        return _http_get_json(base_url, "/api/report/meta", {"month": month})

    def page_fetcher(month: str, version: str, page: int) -> dict:
        return _http_get_json(
            base_url, "/api/report", {"month": month, "version": version, "page": page}
        )

    def parts_fetcher(month: str, version: str, combo: str) -> dict:
        return _http_get_json(
            base_url, "/api/report/parts", {"month": month, "version": version, "combo": combo}
        )

    return meta_fetcher, page_fetcher, parts_fetcher


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MONTHS"] = MONTHS
    app.config["STATION_LABELS"] = {
        "a": STATION_A_LABEL,
        "b": STATION_B_LABEL,
    }

    @app.get("/")
    def index():
        return render_template("index.html", months=MONTHS)

    @app.get("/query")
    def query():
        month = (request.args.get("month") or "").strip()
        raw_k = (request.args.get("k") or "").strip()

        error: str | None = None
        result: dict | None = None

        if not month:
            error = "请选择月份。"
        elif month not in MONTHS:
            error = f"不支持的月份: {month}"
        else:
            try:
                k = int(raw_k)
                if k < 1:
                    raise ValueError
            except ValueError:
                k = 0
                error = "K 必须是不小于 1 的整数。"
            if error is None:
                stations = {
                    STATION_A_LABEL: make_fetchers(STATION_A_URL),
                    STATION_B_LABEL: make_fetchers(STATION_B_URL),
                }
                try:
                    result = top_k(month, k, stations)
                except StationError as exc:
                    # 只提示出错站点，不展示另一站的半截数据；用户可直接重新查询
                    detail = f"：{exc.detail}" if exc.detail else ""
                    error = f"{exc.station} 取数失败{detail}，请稍后重新查询。"

        return render_template(
            "index.html",
            months=MONTHS,
            selected_month=month,
            selected_k=raw_k,
            result=result,
            error=error,
        )

    @app.get("/reconcile")
    def reconcile_view():
        month = (request.args.get("month") or "").strip()

        error: str | None = None
        result: dict | None = None

        if not month:
            error = "请选择月份。"
        elif month not in MONTHS:
            error = f"不支持的月份: {month}"
        else:
            stations = {
                STATION_A_LABEL: make_report_fetchers(STATION_A_URL),
                STATION_B_LABEL: make_report_fetchers(STATION_B_URL),
            }
            try:
                result = reconcile(month, stations)
            except StationError as exc:
                # 只提示出错站点，不展示另一站的半截数据；用户可直接重新查询
                detail = f"：{exc.detail}" if exc.detail else ""
                error = f"{exc.station} 取数失败{detail}，请稍后重新查询。"

        return render_template(
            "reconcile.html",
            months=MONTHS,
            selected_month=month,
            result=result,
            error=error,
        )

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
