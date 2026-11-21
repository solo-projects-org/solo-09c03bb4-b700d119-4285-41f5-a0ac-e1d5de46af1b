# 常备维修包 · 同单更换组合分析

备件组为两处维修站配常备维修包，需要找出最常在同一张工单里一起更换的
零件组合。本项目用 Flask 实现：

* **两个独立的站点服务**（`station/`，同一镜像各部署一份）：只暴露
  分页榜单与编号点查两个受控接口，数据来自各站固定的月度 CSV；
* **一个中心分析网页**（`web/`）：选月份、填 K，按两站次数之和列出
  前 K 个组合，同分按编号字典序，并展示每站次数与实际翻页数；
* **月报核对**（`/reconcile`）：按月份核对两站编号与零件组，找出
  “同编号不同零件组”或“同零件组不同编号”的冲突组，缺定义记录单列待补。

## 目录结构

```
station/
  app.py                 # 站点服务：/api/rank、/api/count、/api/report*
  Dockerfile
  requirements.txt
  data/
    station_a/YYYY-MM.csv         # A 站月度计数（combo_id,count）
    station_a/YYYY-MM.parts.csv   # A 站当月零件字典（combo_id,parts）
    station_b/...                 # B 站月度计数与零件字典
web/
  app.py                 # 分析网页与月报核对页（urllib 调站点接口）
  aggregator.py          # 受限取数下的 Top-K 聚合与早停算法（纯逻辑）
  reconcile.py           # 月报核对：版号取数、并查集连组、冲突明细（纯逻辑）
  templates/index.html
  templates/reconcile.html
  Dockerfile
  requirements.txt
tests/test_aggregator.py # Top-K 算法自测（10 个用例，含随机性质测试）
tests/test_reconcile.py  # 月报核对逻辑自测（9 个用例）
scripts/e2e_check.py     # HTTP 端到端自测脚本
docker-compose.yml       # 三个服务一键启动
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

* 分析网页：http://localhost:8000
* 月报核对：http://localhost:8000/reconcile
* 维修站A 服务：http://localhost:8001
* 维修站B 服务：http://localhost:8002

更换/新增月度数据：把计数 CSV（`YYYY-MM.csv`）与零件字典
（`YYYY-MM.parts.csv`）放进对应 `station/data/station_x/` 目录，
并在 `docker-compose.yml` 的 `AVAILABLE_MONTHS` 里登记月份后重启。
没有 `.parts.csv` 或该文件缺少某编号，就让这些记录进入“待补”。

## 站点接口（中心只能这样取数）

1. `GET /api/rank?month=YYYY-MM&page=N`
   按次数从高到低、每页固定 5 条返回；同分按编号字典序。
   响应含 `items / total / total_pages / has_next`。
2. `GET /api/count?month=YYYY-MM&combo=编号`
   按编号查次数；**缺号计零**。
3. `GET /api/report/meta?month=YYYY-MM`
   返回当月快照**版号** `version`、`total`、`total_pages`。
4. `GET /api/report?month=YYYY-MM&version=版号&page=N`
   按编号分页返回 `combo_id / count / parts_defined / parts_digest`。
   `version` 必须与 meta 一致，否则返回 409（旧版已撤下）。
5. `GET /api/report/parts?month=YYYY-MM&version=版号&combo=编号`
   返回该编号的完整零件清单 `parts`，同样必须带同一版号。

中心无法一次性拿到站点全量数据，只能反复翻页或点查。

## 月报核对规则（web/reconcile.py）

* **版号**：由当月计数与零件字典的规范化内容哈希得到，内容固定则版号
  固定，改任一内容即换版。中心先取 meta 拿到版号，之后**翻页与取清单
  都带同一版号**；站点发现版号已变即拒绝，中心按该站取数失败处理，
  页面只提示出错站点、不展示另一站半截结果。
* **零件组比较**：按零件号集合比较，顺序不限；站点用排序去重后的
  摘要（SHA-256 前 16 位）上报，中心也用同一算法核对。
* **连组**：有定义的记录按“编号相同”或“零件集合摘要相同”连边，
  并查集求连通分量，间接相连的归为一组。
* **冲突判定**：组内出现不止一种（编号, 零件集合）组合即为冲突。
  只对冲突组向站点索取每条记录的完整零件清单，一致组不取清单。
* **待补**：缺零件定义（整月无字典、字典缺该编号或零件列表为空）的
  记录不参与连组，单独列出站点、编号与申报次数。

例：内置 `2026-05` 两站样例中，`COM-100` 是同编号不同零件组；
A 站 `COM-300`（P4、P5）与 B 站 `COM-300`（P8）同编号、又与 B 站
`COM-700`（P4、P5）同零件组，三条记录间接连成一个冲突组；
`COM-200`、`COM-400` 两站一致；`COM-500`（A）、`COM-900`（B）缺定义待补。

## Top-K 与早停算法（web/aggregator.py）

每站维护"已取最后一页的最小次数"作为未取页的次数上界 b。两站轮流
各翻一页，每翻一页就检查能否确认，能确认立即停读：

* 对任意已见组合 c，其最坏情况总和 = 各站已确定次数之和，未在某站
  榜单出现的站按该站上界 b 计；完全陌生的组合最坏为 Σb（编号未知）。
* 当第 K 名之外的所有组合（含陌生组合）在最坏情况下都无法超越当前
  第 K 名（总和严格更小，或总和相等且编号字典序确定靠后），并且
  前 K 名内部相邻次序也不会被未取页数据反超时，名单与次序即确认。
* 确认后，前 K 名里没在某站榜单出现的组合，再用 `/api/count` 点查
  精确次数（缺号为 0）；站点已耗尽则直接判零，不发点查请求。

页面展示每站**实际翻页数**与**点查次数**。任一站点取数失败（连不通、
超时、非法响应等）时，页面只提示出错站点、不展示另一站的半截数据，
服务无状态，直接重新查询即可。

## 本地自测

不使用 Docker 时（需本机有 Python 3.11 与 Flask）：

```bash
# 1) 算法单测（假站点，无需网络）
python3 -I tests/test_aggregator.py

# 2) 起三个进程（端口 8001/8002/8000），再跑 HTTP 端到端
STATION_NAME=维修站A STATION_DATA_DIR=station/data/station_a PORT=8001 \
    python3 station/app.py &
STATION_NAME=维修站B STATION_DATA_DIR=station/data/station_b PORT=8002 \
    python3 station/app.py &
AVAILABLE_MONTHS=2026-01,2026-02,2026-03,2026-04,2026-05 \
STATION_A_URL=http://127.0.0.1:8001 STATION_B_URL=http://127.0.0.1:8002 \
PORT=8000 python3 web/app.py &
python3 -I scripts/e2e_check.py
```

### 两个规定场景的自测结果

* **场景一（`2026-01`, K=3）——总榜第一在两站都没进首屏**：
  Z999 在两站都排第 6 位（首屏各 5 条）。两站首屏里 10 个组合的
  总和都只有 10，陌生组合最坏可到 20，无法确认；翻到第 2 页拿到
  Z999（9+9=18）后边界收紧，确认 Top3 = Z999、A01、A02，
  **两站各翻 2 页后立即停读**。
* **场景二（`2026-02`, K=3）——首屏后就能停读**：
  两站各取 1 页后，陌生组合最坏总和（5+4=9）已严格小于第三名
  C003 的总和 22，**各翻 1 页即停，零次点查**。

## 示例数据说明

内置 5 个月的演示数据：`2026-01`（榜首在首屏之外）、`2026-02`
（首屏即停）、`2026-03`（大量同分验证字典序）、`2026-04`
（未耗尽站点的点查缺号路径）、`2026-05`（月报核对：直接/间接冲突组
与缺定义待补）。`2026-01`～`2026-04` 的核对结果为“两站一致、无冲突”，
可作为无冲突基线。
