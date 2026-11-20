"""分析网页的取数与 Top-K 聚合算法（纯逻辑，不依赖 Flask，便于自测）。

站点接口的约束：
  * 只能按次数从高到低、每页 5 条翻页（rank），或按编号点查（count，缺号计 0）；
  * 中心不能直接读取站点全量数据。

算法目标：在无法一次拿到全量数据的前提下，用尽量少的翻页确认
两站次数之和的前 K 名（同分按编号字典序），一旦名单和次序可确认就停。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

PAGE_SIZE = 5


class StationError(RuntimeError):
    """某个站点取数失败。只携带出错站点信息，便于上层只提示出错站点。"""

    def __init__(self, station: str, detail: str = ""):
        self.station = station
        self.detail = detail
        super().__init__(station, detail)


# rank_fetcher(month, page) -> 站点 rank 接口的 JSON(dict)
# count_fetcher(month, combo) -> 站点 count 接口的 JSON(dict)
RankFetcher = Callable[[str, int], dict]
CountFetcher = Callable[[str, str], dict]


@dataclass
class StationState:
    name: str
    rank_fetcher: RankFetcher
    count_fetcher: CountFetcher
    page: int = 0                      # 已取到第几页
    exhausted: bool = False
    total_pages: Optional[int] = None
    # 已确定次数的组合（榜单页出现过，或点查确认过）-> 该站次数
    seen_counts: dict[str, int] = field(default_factory=dict)
    # 已取最后一页中的最小次数，作为未取页的次数上界
    last_page_min_count: Optional[int] = None

    def unseen_upper_bound(self) -> Optional[int]:
        """尚未翻到的组合在本站次数的上界（含）。

        榜单按次数降序排列，未取页里任何组合的次数都不超过
        已取最后一页中的最小次数；站点已耗尽时上界为 0。
        一页都没取时返回 None（尚无可用上界）。
        """
        if self.exhausted:
            return 0
        return self.last_page_min_count


def _fetch_rank(state: StationState, month: str) -> dict:
    # 调用方在请求前已把 state.page 递增为待取页码
    try:
        payload = state.rank_fetcher(month, state.page)
    except StationError:
        raise
    except Exception as exc:  # 网络错误、超时、非 JSON 等统一归为该站出错
        raise StationError(state.name, str(exc)) from exc
    if not isinstance(payload, dict) or "items" not in payload:
        raise StationError(state.name, "榜单响应格式不正确")
    return payload


def _fetch_count(state: StationState, month: str, combo: str) -> int:
    try:
        payload = state.count_fetcher(month, combo)
    except StationError:
        raise
    except Exception as exc:
        raise StationError(state.name, str(exc)) from exc
    if not isinstance(payload, dict) or "count" not in payload:
        raise StationError(state.name, "点查响应格式不正确")
    try:
        return int(payload["count"])
    except (TypeError, ValueError) as exc:
        raise StationError(state.name, "点查次数格式不正确") from exc


def _rebuild_union(states: dict[str, StationState]) -> dict[str, dict]:
    """用各站已确定的次数重建候选全集（含点查补入的缺号零值）。"""
    union: dict[str, dict] = {}
    for state in states.values():
        for combo, cnt in state.seen_counts.items():
            record = union.setdefault(combo, {"sum": 0, "stations": {}})
            # 同一组合在同一站只会有一个确定值，直接覆盖并由各站值求和
            record["stations"][state.name] = cnt
            record["sum"] = sum(record["stations"].values())
    return union


def _ordered_candidates(union: dict[str, dict]) -> list[tuple[str, int]]:
    """候选组合按 (总次数降序, 编号字典序) 排序。"""
    return sorted(
        ((combo, vals["sum"]) for combo, vals in union.items()),
        key=lambda x: (-x[1], x[0]),
    )


def _cannot_overtake(
    combo: Optional[str],
    worst_sum: int,
    top_k: list[tuple[str, int]],
) -> bool:
    """判断最坏情况下的某个组合是否确定无法进入当前前 K 名。

    排序键为 (总和降序, 编号字典序)。无法超越当且仅当：
      * 最坏总和严格小于第 K 名总和；或
      * 最坏总和等于第 K 名总和，且其编号字典序严格大于第 K 名编号
        （陌生组合没有确定编号，同分时必须保守判定为可能超越）。
    """
    kth_combo, kth_sum = top_k[-1]
    if worst_sum < kth_sum:
        return True
    if worst_sum > kth_sum:
        return False
    if combo is None:
        return False  # 陌生组合可能携带字典序更小的编号
    return combo > kth_combo


def _bound_allows_stop(
    union: dict[str, dict], states: dict[str, StationState], k: int
) -> Optional[list[tuple[str, int]]]:
    """仅依据榜单页数据，检查 Top-K 的名单与次序是否已经可以确认。

    可以确认则返回候选前 K 名 [(combo, total), ...]，否则返回 None。

    判定原理：对任意“已见”组合 c，它在每一站的真实次数要么已经在
    榜单页里确定，要么（该站榜单还没翻到它）不超过该站未取页上界 b_s。
    因此 c 的最坏情况总和 W(c) = 已确定站的真实值 + 未确定站的上界；
    完全没出现过的陌生组合最坏为 Σ_s b_s 且编号未知。当前 K 名之外
    的所有组合（含陌生组合）都确定无法超越第 K 名时即可停读，
    随后再用点查把前 K 名每站次数精确化。
    任一站还没取首页时 b_s 未知（None），不能停。
    """
    bounds = {name: s.unseen_upper_bound() for name, s in states.items()}
    if any(b is None for b in bounds.values()):
        return None

    ordered = _ordered_candidates(union)
    if len(ordered) < k:
        return None
    top_k = ordered[:k]

    # 前 K 内部次序也必须已经确定：相邻两名 i、i+1，即使 i+1 在所有
    # 未确定站都取到上界、i 都取 0，i 仍不能被反超（含同分字典序）。
    for idx in range(len(top_k) - 1):
        hi_combo, _ = top_k[idx]
        lo_combo, _ = top_k[idx + 1]
        lo_worst = 0
        for name, state in states.items():
            if lo_combo in state.seen_counts:
                lo_worst += state.seen_counts[lo_combo]
            else:
                lo_worst += bounds[name]  # type: ignore[operator]
        hi_known = union[hi_combo]["sum"]
        if lo_worst > hi_known or (lo_worst == hi_known and lo_combo < hi_combo):
            return None

    stranger_worst = sum(b for b in bounds.values() if b is not None)
    if not _cannot_overtake(None, stranger_worst, top_k):
        return None

    for combo, _ in ordered[k:]:
        worst = 0
        for name, state in states.items():
            if combo in state.seen_counts:
                worst += state.seen_counts[combo]  # 该站次数已确定
            else:
                worst += bounds[name]  # type: ignore[operator]  # 未取页上界
        if not _cannot_overtake(combo, worst, top_k):
            return None
    return top_k


def _resolve_top_k_counts(
    top_k: list[tuple[str, int]],
    states: dict[str, StationState],
    month: str,
    count_queries: dict[str, int],
) -> list[dict]:
    """对前 K 名逐个站点确认精确次数（榜单没有就点查，缺号计 0）。"""
    results: list[dict] = []
    for combo, _ in top_k:
        entry = {"combo_id": combo, "per_station": {}}
        for state in states.values():
            if combo in state.seen_counts:
                value = state.seen_counts[combo]
            elif state.exhausted:
                value = 0  # 整站榜单都翻过仍未出现，确定为缺号
                state.seen_counts[combo] = 0
            else:
                value = _fetch_count(state, month, combo)
                count_queries[state.name] += 1
                state.seen_counts[combo] = value
            entry["per_station"][state.name] = value
        entry["total"] = sum(entry["per_station"].values())
        results.append(entry)
    results.sort(key=lambda e: (-e["total"], e["combo_id"]))
    return results


def top_k(
    month: str,
    k: int,
    stations: dict[str, tuple[RankFetcher, CountFetcher]],
    max_pages: int = 10000,
) -> dict:
    """聚合两个站点的数据，返回 Top-K 及每站实际翻页数。

    stations: {站点名: (rank_fetcher, count_fetcher)}
    返回: {"month", "k", "results", "pages_fetched": {站点: 页数},
           "count_queries": {站点: 点查次数}}
    任一站取数失败即抛 StationError，只携带出错站点，上层可重新查询。
    """
    if k < 1:
        raise ValueError("k 必须是正整数")
    if not stations:
        raise ValueError("至少需要一个站点")

    states = {
        name: StationState(name=name, rank_fetcher=rf, count_fetcher=cf)
        for name, (rf, cf) in stations.items()
    }
    count_queries = {name: 0 for name in states}
    station_names = list(stations.keys())

    def ingest_page(state: StationState, items: list[dict]) -> None:
        page_min_count: Optional[int] = None
        for item in items:
            combo = str(item["combo_id"])
            cnt = int(item["count"])
            state.seen_counts[combo] = cnt
            if page_min_count is None or cnt < page_min_count:
                page_min_count = cnt
        state.last_page_min_count = page_min_count

    # 两站轮流翻页，每取完一页就检查能否确认；确认成功立即停止
    while True:
        # 点查补入的次数也会写入 seen_counts，每轮统一重建保证总和最新
        union = _rebuild_union(states)
        candidate_top = _bound_allows_stop(union, states, k)
        if candidate_top is not None:
            results = _resolve_top_k_counts(
                candidate_top, states, month, count_queries
            )
            break

        if all(s.exhausted for s in states.values()):
            # 两站全部翻完仍不足 K 个：数据本身就这么多，全部按规则返回
            all_ordered = _ordered_candidates(union)
            results = _resolve_top_k_counts(
                all_ordered, states, month, count_queries
            )
            break

        progressed = False
        for name in station_names:
            state = states[name]
            if state.exhausted:
                continue
            if state.page >= max_pages:
                raise StationError(name, f"翻页超过上限 {max_pages}")
            state.page += 1
            payload = _fetch_rank(state, month)
            page_items = payload.get("items") or []
            state.total_pages = payload.get("total_pages")
            ingest_page(state, page_items)
            has_next = payload.get("has_next")
            if has_next is None:
                has_next = (
                    state.total_pages is not None
                    and state.page < state.total_pages
                )
            if not has_next or not page_items:
                state.exhausted = True
            progressed = True

            candidate_top = _bound_allows_stop(_rebuild_union(states), states, k)
            if candidate_top is not None:
                results = _resolve_top_k_counts(
                    candidate_top, states, month, count_queries
                )
                break
        else:
            # for 正常跑完未 break：继续 while 下一轮（由 progressed 兜底防死循环）
            if not progressed:
                raise RuntimeError("无法继续取数且未能确认结果")
            continue
        break

    return {
        "month": month,
        "k": k,
        "results": results,
        "pages_fetched": {name: states[name].page for name in states},
        "count_queries": count_queries,
    }
