"""月报核对逻辑自测（不依赖网络，用内存假站点模拟同版快照接口）。

覆盖：
  1. 同编号不同零件组 -> 冲突组，且只对冲突组取完整清单；
  2. 同零件组不同编号 -> 冲突组；
  3. 间接相连（编号边 + 零件边串起来）归入同一组；
  4. 完全一致的组不取清单；
  5. 缺定义记录单列待补，不参与连组；
  6. 零件顺序不同但集合相同 -> 摘要一致，不算冲突；
  7. 取明细时站点撤版 -> StationError，只带出错站点；
  8. 翻页时版号变化 -> 失败，不混用两版；
  9. 两站版号都随内容固定，同内容重复取数版号不变。
"""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

from aggregator import StationError  # noqa: E402
from reconcile import parts_digest, reconcile  # noqa: E402

PAGE_SIZE = 5


class FakeReportStation:
    """内存站点：提供 meta / 同版分页 / 同版清单，行为对齐真实接口。"""

    def __init__(self, name, counts=None, parts=None, fail_pages=(), withdraw_parts=False):
        self.name = name
        self.counts = dict(counts or {})
        self.parts = {k: list(v) for k, v in (parts or {}).items()}
        self.fail_pages = set(fail_pages)
        self.withdraw_parts = withdraw_parts
        self.version = self._compute_version()
        self.page_calls = []
        self.parts_calls = []

    def _compute_version(self):
        canonical = sorted(
            [["count", cid, cnt] for cid, cnt in self.counts.items()]
            + [["parts", cid, sorted(set(ps))] for cid, ps in self.parts.items()]
        )
        return hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()[:16]

    def rotate_version(self):
        self.counts = {cid: cnt + 1 for cid, cnt in self.counts.items()}
        self.version = self._compute_version()

    def meta(self, month):
        total = len(self.counts)
        return {
            "station": self.name,
            "month": month,
            "version": self.version,
            "total": total,
            "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 0,
        }

    def page(self, month, version, page):
        self.page_calls.append((version, page))
        if page in self.fail_pages:
            raise RuntimeError("模拟连接中断")
        if version != self.version:
            raise RuntimeError(f"已无版号 {version}")
        ids = sorted(self.counts)
        start = (page - 1) * PAGE_SIZE
        items = []
        for cid in ids[start:start + PAGE_SIZE]:
            pset = self.parts.get(cid) or []
            items.append({
                "combo_id": cid,
                "count": self.counts[cid],
                "parts_defined": bool(pset),
                "parts_digest": parts_digest(pset) if pset else None,
            })
        return {"station": self.name, "version": self.version, "page": page, "items": items}

    def parts_for(self, month, version, combo):
        self.parts_calls.append((version, combo))
        if self.withdraw_parts:
            raise RuntimeError(f"已无版号 {version}")
        if version != self.version:
            raise RuntimeError(f"已无版号 {version}")
        parts = self.parts.get(combo) or []
        if not parts:
            raise RuntimeError("缺少零件定义")
        return {
            "station": self.name,
            "version": self.version,
            "combo_id": combo,
            "parts": parts,
            "parts_digest": parts_digest(parts),
        }

    def fetchers(self):
        return self.meta, self.page, self.parts_for


def build(stations: dict[str, FakeReportStation]):
    return {name: fake.fetchers() for name, fake in stations.items()}


MAY = {
    "维修站A": FakeReportStation(
        "维修站A",
        counts={"COM-100": 12, "COM-200": 8, "COM-300": 6, "COM-400": 5, "COM-500": 3},
        # COM-300 用 P5;P4 书写，集合与 B 的 COM-700 相同
        parts={
            "COM-100": ["P1", "P2"], "COM-200": ["P7"],
            "COM-300": ["P5", "P4"], "COM-400": ["P10"],
        },
    ),
    "维修站B": FakeReportStation(
        "维修站B",
        counts={"COM-100": 9, "COM-200": 7, "COM-300": 4, "COM-700": 2,
                "COM-400": 3, "COM-900": 1},
        parts={
            "COM-100": ["P1", "P3"], "COM-200": ["P7"],
            "COM-300": ["P8"], "COM-700": ["P4", "P5"], "COM-400": ["P10"],
        },
    ),
}


def fresh_may():
    return {
        "维修站A": FakeReportStation(
            "维修站A",
            counts={"COM-100": 12, "COM-200": 8, "COM-300": 6, "COM-400": 5, "COM-500": 3},
            parts={
                "COM-100": ["P1", "P2"], "COM-200": ["P7"],
                "COM-300": ["P5", "P4"], "COM-400": ["P10"],
            },
        ),
        "维修站B": FakeReportStation(
            "维修站B",
            counts={"COM-100": 9, "COM-200": 7, "COM-300": 4, "COM-700": 2,
                    "COM-400": 3, "COM-900": 1},
            parts={
                "COM-100": ["P1", "P3"], "COM-200": ["P7"],
                "COM-300": ["P8"], "COM-700": ["P4", "P5"], "COM-400": ["P10"],
            },
        ),
    }


class ReconcileTests(unittest.TestCase):
    def test_direct_and_indirect_conflicts_grouping(self):
        result = reconcile("2026-05", build(fresh_may()))
        self.assertEqual(result["conflict_count"], 2)
        sizes = [len(g["records"]) for g in result["conflict_groups"]]
        self.assertEqual(sizes, [3, 2])  # 间接冲突组较大，排在前面

        # 间接组：A 的 COM-300 与 B 的 COM-300 同编号；A 的 COM-300 与
        # B 的 COM-700 零件集合相同，三条记录归在一起
        big = result["conflict_groups"][0]
        self.assertEqual(big["combo_ids"], ["COM-300", "COM-700"])
        got = {(r["station"], r["combo_id"]): r["parts"] for r in big["records"]}
        self.assertEqual(got[("维修站A", "COM-300")], ["P4", "P5"])
        self.assertEqual(got[("维修站B", "COM-300")], ["P8"])
        self.assertEqual(got[("维修站B", "COM-700")], ["P4", "P5"])

        # 直接组：同编号 COM-100 但零件组不同
        direct = result["conflict_groups"][1]
        self.assertEqual(direct["combo_ids"], ["COM-100"])
        self.assertEqual({r["station"] for r in direct["records"]},
                         {"维修站A", "维修站B"})
        # 完整零件清单按集合返回，与书写顺序无关
        a_rec = next(r for r in direct["records"] if r["station"] == "维修站A")
        self.assertEqual(a_rec["parts"], ["P1", "P2"])
        self.assertEqual(a_rec["count"], 12)

    def test_only_conflict_groups_fetch_parts(self):
        fakes = fresh_may()
        result = reconcile("2026-05", build(fakes))
        called = {(name, combo) for name, f in fakes.items() for _, combo in f.parts_calls}
        # COM-200 / COM-400 一致，不应取清单；缺定义 COM-500 / COM-900 也不取
        self.assertEqual(called, {
            ("维修站A", "COM-100"), ("维修站B", "COM-100"),
            ("维修站A", "COM-300"), ("维修站B", "COM-300"),
            ("维修站B", "COM-700"),
        })
        self.assertEqual(result["consistent_group_count"], 2)
        groups_by_id = {
            tuple(sorted({r["combo_id"] for r in g["records"]})): g
            for g in result["conflict_groups"]
        }
        self.assertEqual(set(groups_by_id), {("COM-100",), ("COM-300", "COM-700")})

    def test_pending_missing_definitions_listed_separately(self):
        result = reconcile("2026-05", build(fresh_may()))
        pending = {(r["station"], r["combo_id"]) for r in result["pending"]}
        self.assertEqual(pending, {("维修站A", "COM-500"), ("维修站B", "COM-900")})
        self.assertEqual(result["pending_count"], 2)
        for group in result["conflict_groups"]:
            for rec in group["records"]:
                self.assertNotIn(rec["combo_id"], ("COM-500", "COM-900"))

    def test_identical_groups_do_not_conflict(self):
        a = FakeReportStation("维修站A", counts={"X": 3, "Y": 1},
                              parts={"X": ["P1", "P2"], "Y": ["P9"]})
        b = FakeReportStation("维修站B", counts={"X": 2, "Y": 4},
                              parts={"X": ["P2", "P1"], "Y": ["P9"]})
        result = reconcile("2026-06", build({"维修站A": a, "维修站B": b}))
        self.assertEqual(result["conflict_groups"], [])
        self.assertEqual(result["pending"], [])
        self.assertEqual(a.parts_calls, [])  # 无冲突不取明细
        self.assertEqual(b.parts_calls, [])

    def test_parts_order_insensitive(self):
        a = FakeReportStation("维修站A", counts={"X": 1}, parts={"X": ["P2", "P1"]})
        b = FakeReportStation("维修站B", counts={"X": 1}, parts={"X": ["P1", "P2"]})
        result = reconcile("2026-06", build({"维修站A": a, "维修站B": b}))
        self.assertEqual(result["conflict_count"], 0)
        self.assertEqual(parts_digest(["P2", "P1"]), parts_digest(["P1", "P2"]))

    def test_version_withdrawn_during_parts_fetch_reports_only_station(self):
        fakes = fresh_may()
        fakes["维修站B"].withdraw_parts = True
        with self.assertRaises(StationError) as ctx:
            reconcile("2026-05", build(fakes))
        self.assertEqual(ctx.exception.station, "维修站B")
        self.assertNotIn("维修站A", str(ctx.exception))
        # 撤版发生在取明细阶段：分页都已完成
        self.assertEqual(fakes["维修站A"].page_calls,
                         [(fakes["维修站A"].version, 1)])
        self.assertTrue(fakes["维修站B"].parts_calls)

    def test_version_changes_between_pages_fails(self):
        fakes = fresh_may()
        orig_page = fakes["维修站B"].page

        def rotating_page(month, version, page):
            if page == 2:
                fakes["维修站B"].rotate_version()  # 翻页中途换版
            return orig_page(month, version, page)

        meta, _, parts = fakes["维修站B"].fetchers()
        stations = build(fakes)
        stations["维修站B"] = (meta, rotating_page, parts)
        with self.assertRaises(StationError) as ctx:
            reconcile("2026-05", stations)
        self.assertEqual(ctx.exception.station, "维修站B")
        self.assertIn("版号", ctx.exception.detail)

    def test_version_is_stable_for_same_content(self):
        fakes = fresh_may()
        first = reconcile("2026-05", build(fakes))
        fakes2 = fresh_may()
        second = reconcile("2026-05", build(fakes2))
        for label in ("维修站A", "维修站B"):
            self.assertEqual(first["stations"][label]["version"],
                             second["stations"][label]["version"])
        # 内容变化 -> 版号变化
        fakes2["维修站A"].counts["COM-100"] += 1
        changed = fakes2["维修站A"]._compute_version()
        self.assertNotEqual(changed, first["stations"]["维修站A"]["version"])

    def test_totals_pages_report(self):
        fakes = fresh_may()
        result = reconcile("2026-05", build(fakes))
        self.assertEqual(result["stations"]["维修站A"]["pages_fetched"], 1)
        self.assertEqual(result["stations"]["维修站B"]["pages_fetched"], 2)
        self.assertEqual(result["stations"]["维修站A"]["total"], 5)
        self.assertEqual(result["stations"]["维修站B"]["total"], 6)
        self.assertEqual(result["defined_record_count"], 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
