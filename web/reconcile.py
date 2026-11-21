"""月报核对：按月份找出两站之间需要一起修正的编号组（纯逻辑，便于自测）。

约束与规则：
  * 每站当期有一个版号，标识“计数 + 零件字典”的固定快照；中心翻页与
    取零件清单都必须携带同一个版号。站点查不到该版号（已撤版）按取数
    失败处理，只报出错站点。
  * 中心分页取“有申报的编号、次数、零件集合摘要”，不直接拿全量文件。
  * 有零件定义的记录，按“编号相同”或“零件集合摘要相同”连边（并查集），
    间接相连的也归为一组。零件组按零件号集合比较，与顺序无关。
  * 组内出现不止一种（编号, 零件集合）组合即为冲突组。只有冲突组才向
    站点索取每条记录的完整零件清单；一致组不取清单。
  * 缺零件定义的记录不参与连组，单独列为“待补”。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from aggregator import StationError

DIGEST_LEN = 16


def parts_digest(parts: list[str]) -> str:
    """与站点一致的零件集合摘要：排序去重后哈希，顺序不影响结果。"""
    canonical = "\x1f".join(sorted(set(parts)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_LEN]


# meta_fetcher(month) -> {"version", "total", "total_pages"}
# page_fetcher(month, version, page) -> {"version", "items": [...]}
# parts_fetcher(month, version, combo) -> {"version", "parts": [...]}
MetaFetcher = Callable[[str], dict]
PageFetcher = Callable[[str, str, int], dict]
PartsFetcher = Callable[[str, str, str], dict]


@dataclass
class _Record:
    station: str
    combo_id: str
    count: int
    digest: str


class _DSU:
    """并查集：按编号或零件集合摘要把记录连成组。"""

    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


@dataclass
class _StationData:
    label: str
    version: str
    pages_fetched: int = 0
    total: int = 0
    items: list[dict] = field(default_factory=list)


def _as_int(value, field_name: str, station: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise StationError(station, f"响应字段 {field_name} 不是整数") from exc
    return result


def _fetch_station(
    label: str,
    month: str,
    meta_fetcher: MetaFetcher,
    page_fetcher: PageFetcher,
    max_pages: int,
) -> _StationData:
    """用同一版号翻完某站当月所有页，收集编号/次数/零件集合摘要。"""
    try:
        meta = meta_fetcher(month)
    except StationError:
        raise
    except Exception as exc:
        raise StationError(label, str(exc)) from exc
    if not isinstance(meta, dict):
        raise StationError(label, "月报元信息格式不正确")
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        raise StationError(label, "月报元信息缺少版号")
    total = _as_int(meta.get("total"), "total", label)
    total_pages = _as_int(meta.get("total_pages"), "total_pages", label)
    if total < 0 or total_pages < 0:
        raise StationError(label, "月报元信息数值为负")
    if total_pages > max_pages:
        raise StationError(label, f"分页超过上限 {max_pages}")

    data = _StationData(label=label, version=version, total=total)
    for page in range(1, total_pages + 1):
        try:
            payload = page_fetcher(month, version, page)
        except StationError:
            raise
        except Exception as exc:
            raise StationError(label, str(exc)) from exc
        if not isinstance(payload, dict):
            raise StationError(label, "月报分页响应格式不正确")
        if payload.get("version") != version:
            # 翻页期间站点换了版：本次不能混用两版数据
            raise StationError(
                label,
                f"翻页时版号不一致（期望 {version}，实际 {payload.get('version')}）",
            )
        items = payload.get("items")
        if not isinstance(items, list):
            raise StationError(label, "月报分页响应缺少 items")
        data.pages_fetched += 1
        for item in items:
            if not isinstance(item, dict):
                raise StationError(label, "月报分页条目格式不正确")
            combo = item.get("combo_id")
            if not isinstance(combo, str) or not combo:
                raise StationError(label, "月报分页条目缺少编号")
            count = _as_int(item.get("count"), "count", label)
            defined = bool(item.get("parts_defined"))
            digest = item.get("parts_digest")
            if defined and (not isinstance(digest, str) or not digest):
                raise StationError(label, f"编号 {combo} 有定义但缺少零件摘要")
            data.items.append({
                "combo_id": combo,
                "count": count,
                "parts_defined": defined,
                "parts_digest": digest if defined else None,
            })
    if len(data.items) != total:
        raise StationError(
            label, f"分页收集到 {len(data.items)} 条，与站点声明的 {total} 条不一致"
        )
    # 同站同编号只应有唯一一条记录
    combos = [item["combo_id"] for item in data.items]
    if len(set(combos)) != len(combos):
        raise StationError(label, "分页结果中编号重复")
    return data


def _assign_groups(
    records: list[_Record],
) -> list[dict]:
    """用并查集把有定义的记录按相同编号/相同零件集合连成组。"""
    dsu = _DSU(len(records))
    first_by_id: dict[str, int] = {}
    first_by_digest: dict[str, int] = {}
    for idx, rec in enumerate(records):
        prev = first_by_id.setdefault(rec.combo_id, idx)
        dsu.union(prev, idx)
        prev = first_by_digest.setdefault(rec.digest, idx)
        dsu.union(prev, idx)

    buckets: dict[int, list[_Record]] = {}
    for idx, rec in enumerate(records):
        buckets.setdefault(dsu.find(idx), []).append(rec)

    groups: list[dict] = []
    for members in buckets.values():
        members.sort(key=lambda r: (r.station, r.combo_id))
        pairs = {(r.combo_id, r.digest) for r in members}
        groups.append({
            "records": members,
            "conflict": len(pairs) > 1,
        })
    groups.sort(
        key=lambda g: (
            not g["conflict"],
            -len(g["records"]),
            min(r.combo_id for r in g["records"]),
        )
    )
    return groups


def _fetch_parts_checked(
    label: str,
    month: str,
    version: str,
    combo: str,
    expected_digest: str,
    parts_fetcher: PartsFetcher,
) -> list[str]:
    """取某记录的完整零件清单，并核对版号与摘要。"""
    try:
        payload = parts_fetcher(month, version, combo)
    except StationError:
        raise
    except Exception as exc:
        raise StationError(label, str(exc)) from exc
    if not isinstance(payload, dict):
        raise StationError(label, "零件清单响应格式不正确")
    if payload.get("version") != version:
        raise StationError(
            label,
            f"取零件清单时版号不一致（期望 {version}，实际 {payload.get('version')}）",
        )
    parts = payload.get("parts")
    if not isinstance(parts, list) or not parts or any(
        not isinstance(p, str) or not p for p in parts
    ):
        raise StationError(label, f"编号 {combo} 的零件清单格式不正确")
    digest = parts_digest(parts)
    if digest != expected_digest:
        raise StationError(
            label, f"编号 {combo} 的零件清单与摘要不一致（版号 {version}）"
        )
    if payload.get("parts_digest") not in (None, digest):
        raise StationError(label, f"编号 {combo} 的零件摘要校验失败")
    return sorted(set(parts))


def reconcile(
    month: str,
    stations: dict[str, tuple[MetaFetcher, PageFetcher, PartsFetcher]],
    max_pages: int = 1000,
) -> dict:
    """核对两站月报，返回冲突组、待补记录与各站版号。

    stations: {站点名: (meta_fetcher, page_fetcher, parts_fetcher)}
    任一站读不到版号或取数失败即抛 StationError，只携带出错站点。
    """
    if not stations:
        raise ValueError("至少需要一个站点")

    station_data: dict[str, _StationData] = {}
    for label, (meta_fetcher, page_fetcher, _) in stations.items():
        station_data[label] = _fetch_station(
            label, month, meta_fetcher, page_fetcher, max_pages
        )

    defined: list[_Record] = []
    pending: list[dict] = []
    for label, data in station_data.items():
        for item in data.items:
            if item["parts_defined"]:
                defined.append(_Record(
                    station=label,
                    combo_id=item["combo_id"],
                    count=item["count"],
                    digest=item["parts_digest"],
                ))
            else:
                pending.append({
                    "station": label,
                    "combo_id": item["combo_id"],
                    "count": item["count"],
                })

    groups = _assign_groups(defined)

    conflict_groups: list[dict] = []
    consistent_groups = 0
    conflict_index = 0
    for group in groups:
        if not group["conflict"]:
            consistent_groups += 1
            continue
        conflict_index += 1
        fetched: list[dict] = []
        for rec in group["records"]:
            _, _, parts_fetcher = stations[rec.station]
            parts = _fetch_parts_checked(
                rec.station,
                month,
                station_data[rec.station].version,
                rec.combo_id,
                rec.digest,
                parts_fetcher,
            )
            fetched.append({
                "station": rec.station,
                "combo_id": rec.combo_id,
                "count": rec.count,
                "parts": parts,
            })
        conflict_groups.append({
            "index": conflict_index,
            "combo_ids": sorted({r.combo_id for r in group["records"]}),
            "stations": sorted({r.station for r in group["records"]}),
            "records": fetched,
        })

    pending.sort(key=lambda r: (r["station"], r["combo_id"]))

    return {
        "month": month,
        "stations": {
            label: {
                "version": data.version,
                "pages_fetched": data.pages_fetched,
                "total": data.total,
            }
            for label, data in station_data.items()
        },
        "conflict_groups": conflict_groups,
        "pending": pending,
        "conflict_count": len(conflict_groups),
        "consistent_group_count": consistent_groups,
        "defined_record_count": len(defined),
        "pending_count": len(pending),
    }
