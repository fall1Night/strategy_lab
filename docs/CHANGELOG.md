# 变更日志 · Strategy Lab

> 截至 **2026-07-19**，按模块归类记录本项目累积的所有功能性改动。
> 详细设计见 `docs/` 下对应文档：使用指南 / 需求文档 / 技术文档 / system_design / src-layout-refactor-design / **v2.0-design（v2.0 完整设计方案）**。
> 版本线：v1.0 基线（策略回测引擎 + CLI + 零依赖网页）→ 存储改造 → 包化重构 → Web 交互增强 → **v2.0 批量扫描 + 数据仓库化（规划中）**。

---

## 一、存储与架构

### 1. 回测结果落库（数据库存储改造）
- 结果从「按标的命名的 `equity/trades/summary` 三件套文件」改为 **统一落库**（SQLAlchemy 2.0，DB 无关）。
- `run_id = UUID4`（粒度 = 单标的 × 策略 × 区间）；五张表：`backtest_runs` / `equity_points` / `trades` / `summary` / `schema_version`。
- 决策：**仅落库，不双写文件**；DB 不可用时明确报错终止（不再静默回退文件）。
- 新增端点：`GET /history`、`GET /api/runs`、`GET /api/runs/<id>`、`POST /compare`（跨回测对比，各 run 权益起点 **rebased 到 100**）。
- 默认 SQLite（`strategy_lab.db`，零额外依赖）；可切 MySQL/PostgreSQL（配 `DATABASE_URL` + 对应 driver）。

### 2. 代码包化重构（src/ 布局）
- 改为专业 Python 包：`src/strategylab/`，`pyproject.toml`（PEP 621 / setuptools）。
- 生成命令：`strategylab`（CLI）、`strategylab-web`（网页）；`python -m strategylab[.web]` 等价写法。
- 删除 `run.py` 的 `sys.path` hack；清理根目录死目录；新增 `LICENSE`（MIT）、`.gitignore`、更新 `README.md`。

---

## 二、网页交互（Web UI）

### 3. 标的实时搜索（基线已有，本次同步进文档）
- 输入框打字 → `GET /api/search` → 东方财富全 A 股列表（10 分钟缓存）模糊过滤，弹候选浮层，点选填入；支持多标的。

### 4. 按板块选股（新增功能）
- 表单新增「**按板块选股**」下拉：**31 个申万一级行业**（半导体 / 白酒 / 化学制药 等）。
- 选定行业弹出**成分股勾选面板**，勾选成分股（**单次最多 10 只**），可「✕ 全清」一键清空。
- 数据来源 `data/sector_stocks.json`（约 **5527 只**，构建期一次性拉取缓存）；后端 `GET /api/sector-stocks?code=<行业code>` 读取返回。
- 勾选标的与搜索框标的合并进入回测。

### 5. 界面统一显示股票名称（而非代码）
- 所有用户可见位置（搜索候选、已选标的、左侧历史、对比页标签、持仓明细）一律显示**股票名称**（如「浙江医药」），代码仅在后台对应。
- 提交时前端分离「显示名 `real-names`」与「后台代码 `real-symbols`」，杜绝中文名被误当代码。

### 6. 日期快捷选择按钮（新增）
- 日期框旁「**最近 1 年 / 最近 3 年 / 最近 5 年**」按钮，一键填入对应起止日期（以今天为终点往前推）。

### 7. 持仓明细表新增「做 T 收益率对比」两列（新增）
- `持仓不动收益率`（`hold_no_t_pct`）：建仓持有到清仓、完全不做 T 的回报。
- `T持有到清仓收益率`（`t_hold_to_exit_pct`）：含做 T 的全部回报（≈ 总收益率）。
- 两列之差 = 做 T 对单笔持仓的增厚 / 拖累，直观可读。

### 8. 持仓明细表横向滚动条优化（UI 修复）
- `position_table` 原复用交易表的 `.trades-table`（含 `min-width:720px` + `white-space:nowrap` + `overflow-x:auto`），14 列被撑宽 → 底部横滚条。
- 新增独立 `.position-table` / `.position-table-wrap`：容器 `overflow-x:hidden`、表格 `width:100%` 无 `min-width`、单元格 `white-space:normal` 允许换行；`trades_table` 行为不变。
- 已知取舍：极窄视口（<720px）下 `overflow-x:hidden` 为「裁剪」而非「滚动」；主流桌面宽度正常铺开无滚动条。

---

## 三、策略

### 9. 默认策略重命名
- 展示名统一为 **「周线 MACD + 日线 KDJ 双入口做T」**（CLI help / 网页提示 / README / 配置 `name` 一致）。

---

## 四、Bug 修复（Web 提交链路）

### 10. 中文代码 / 名称相关
- **`UnicodeEncodeError`**：仅用板块面板、未搜索时 `window._stockMap` 未初始化，中文名泄漏为代码提交 → 后端报错。修复：后端守卫拒绝含中文的代码 + 前端 IIFE 初始化即绑定 `window._stockMap = _stockMap`。
- **`ValueError: 标的代码包含中文`**：`window._stockMap` 仅在搜索回调内赋值，导致 `syncSectorSelections` 与 `updateHidden` 引用两个不同对象。修复：IIFE 起始处 `var _stockMap = {}; window._stockMap = _stockMap;` 统一为同一对象。

### 11. 重复选股
- 板块多选时同一股票被重复选入标的。修复：去重用 `Object.values(_sectorSelections)` 构造 `seen` 集合（原为按代码 key 误判）。

### 12. 板块成分股联动
- 选板块后勾选成分股未与板块正确联动、日期快捷失效。修复：组件初始化时机与事件绑定修正，确保面板随板块切换刷新。

---

## 五、文档同步
- 新建 `docs/使用指南.md`（手把手中文指南：安装 / CLI / 网页 / 历史对比 / 改参 / FAQ）。
- 更新 `docs/需求文档.md`（FR-01~11，含板块选股、名称显示、做 T 收益率列）。
- 更新 `docs/技术文档.md`（模块职责、网页流程、`/api/sector-stocks`、positions 新增字段）。
- 新建 `docs/system_design.md`（存储改造设计 + v2 落地差异）、`docs/src-layout-refactor-design.md`（包化设计）。
- 本文件 `docs/CHANGELOG.md`（变更汇总）。

---

## 六、v2.0 大更新（批量扫描 + 数据仓库化）— 规划中（2026-07-19）

> 核心理念：**两页解耦（数据生产 `/production` vs 数据分析 `/analysis`）+ 一次跑多次查**。
> 用户诉求：从「手动选股 → 单次回测 → 对比」升级为「选策略 → 一键扫描一批股票 → 直观看各股票收益率排名」。
> 铁律：不引入 Redis/Celery 等重型中间件，仅用 Python 标准库 + 现有 MySQL。

### 13. 数据库迁移 v1→v2（FR-12）
- 新增 `batches` 表（批次元信息：strategy/params_hash/scope/计数/status/时间戳）。
- 新增 `batch_items` 表（每只标的状态：symbol/sector_code/status/run_id/is_reused/error）。
- `backtest_runs` 新增 `params_hash` 字段+索引，老数据回填。
- `init_db()` 启动时检测 schema 版本，v1 自动迁移到 v2（幂等）。

### 14. 命中复用机制（FR-13/14）
- `params_hash = sha1(规范化 params)`，调参后 hash 变化视为新 run。
- 提交批次时预查 `strategy_name + symbol + params_hash`，命中的标 `skipped` 关联已有 run，未命中的入队执行。
- 回测区间**固定 2020-01-01 ~ 今天**，前端去掉日期选择器。

### 15. 批量扫描后台执行（FR-15/16/17）
- 新增 `engine/batch_runner.py`：`ThreadPoolExecutor(max_workers=8)` 后台并发；内存 `threading.Event` 取消标志；单只失败不终止整批；原子 SQL 更新计数。
- `save_run` 改用 `bulk_insert_mappings` 批量写入（快 10-50 倍）。
- 新增 API：`POST /api/batch`、`GET /api/batch/<id>/progress`、`POST /api/batch/<id>/cancel`、`GET /api/batch`。

### 16. 数据生产页（FR-18）
- 新增 `/production`：表单（策略+范围，无日期）+ 进度区（2s 轮询）+ 板块总览区（31 板块 ✅/⏳/未跑，策略×板块维度）+ 取消按钮。
- 范围：按板块 / 自定义池 / 全市场预热。

### 17. 缓存 bug 修复（FR-19）
- 修复历史 bug：缓存 key 不含区间，换区间会用截断数据。
- 新增 `<prefix>_meta.json` 记录区间，请求区间⊆缓存区间才复用；`tempfile + os.replace` 原子写。

### 18. 分析查询页与排名（FR-20/21）
- 新增 `/analysis`：表单（策略+范围，无日期）+ 排名表（股票名+总收益+回撤+夏普，降序分页 50/页）+ 点行进详情。
- `GET /api/rank` 只返回已落库 run，顶部显示「共 N 只已跑过，还有 M 只未跑」。

### 19. 板块总览与全市场预热（FR-22/23）
- `GET /api/sector-status` 按「策略×板块」维度聚合返回 31 板块完成状态。
- `POST /api/batch/warmup-all` 31 板块成分股全部入队，单线程池跑完。

### 20. 导航与服务重启（FR-24/25）
- 顶部导航栏：首页/快速回测（/）· 数据生产（/production）· 分析查询（/analysis）· 回测历史（/history）。`/` 降级为快速回测。
- 服务重启：未完成批次标 `interrupted`，可「重新发起（自动复用已完成）」。

### 21. P2 增强（FR-26~31）
- MySQL 连接池调优、进程内 symbol 锁、老缓存 meta 补建脚本、批次历史 UI、排名导出 CSV、数据时效提示（end 距今>30 天标注「较旧」）。

### 文档同步（v2.0）
- `docs/需求文档.md`：版本升 v2.0，新增 FR-12~FR-31（P0/P1/P2 分组），更新 §6 范围。
- `docs/技术文档.md`：版本升 v2.0，新增 §15 v2.0 架构扩展（数据模型/命中复用/API/batch_runner/缓存修复/并发性能/前端两页）。
- `docs/使用指南.md`：新增 §10.5 v2.0 批量扫描与排名（数据生产页/分析查询页操作说明，大白话）。
- **`docs/v2.0-design.md`（NEW）：v2.0 完整设计方案汇总**（升级背景、已确认决策、数据模型、命中复用算法、API、前端两页、执行流程、并发性能、缓存修复、任务清单、文件清单、风险）。
- 本文件追加「六、v2.0 大更新」章节。

---

## 七、2026-07-19 多数据源抽象层 + 风控增强（完整 SOP 交付）

### 22. 多数据源抽象层（PRD + 架构 + 实现 + QA 全链路）
- **PRD**（许清楚）：`docs/multidatasource-prd.md` — 产品目标 G1~G3、用户故事、P0/P1/P2 需求池、`.env+CLI` 配置方案。
- **架构**（高见远）：`docs/multidatasource-arch.md` + `multidatasource-class.mermaid` + `multidatasource-sequence.mermaid` — 抽象基类(模板方法)+适配器+工厂+容灾策略四件套。
- **实现**（寇豆码 IS_PASS: YES）：13 个新建文件（`src/strategylab/engine/datasource/` 子包）+ 10 个修改文件。
  - 东财 100% 保留为默认源，限流/熔断改为每源独立。
  - `data_feed.py` 改造为兼容层 shim，调用方零改动。
  - 缓存文件名带 source（`<prefix>_<source>_<period>.csv`），旧缓存兼容回退。
  - 运行复用键扩展为含 `data_source` 字段 + 历史回填 eastmoney。
- **QA**（严过关 74/74 NoOne）：全量回归测试覆盖导入/兼容层/缺依赖报错/东财适配器/工厂配置/缓存 key/CLI/DB 迁移。

### 23. 收益率颜色统一（前端）
- 将全平台收益率颜色统一为：**正收益红色 `#f87171`、负收益绿色 `#4ade80`**（与详情页一致）。
- 涉及侧边栏 `.rval`、分析页 `.rtab`、历史页列表共 3 处 CSS 修复 + 历史页补加颜色类。
- 新增 `Cache-Control: no-store` 头，防止浏览器样式缓存导致用户看不到更新。

### 24. 启动体验优化
- 新增 `src/strategylab/__main__.py`：`python -m strategylab` 等价于 `python -m strategylab.web`。
- 新增 `restart.bat`（纯英文，双击即用）：自动杀 8000 端口旧进程 → 等待释放 → 重启服务。
- 新增 `restart.sh`（Git Bash 用户专享）。
- `ThreadingHTTPServer.allow_reuse_address = True`：解决快速重启时 TIME_WAIT 导致的端口绑定失败。

### 25. 取消按钮交互优化（前端）
- `cancelBatch()` 改为：立即停止轮询 → 按钮禁用/变灰 → 显示"取消请求已发送"，不再只会 alert。
- 新增 `_cancelling` 状态锁，防止重复点击。

### 26. 策略修改：峰值回撤清仓 + 做T确认两万
- 新增清仓条件：持仓期间监控总资产峰值，从最高点回落 ≥ 6% 时强制清仓。与原 MACD 水上死叉条件为"或"关系，满足其一即清。
- 建仓时设初始峰值、清仓时复位、做T加仓自动更新峰值。
- 做T单笔买入额确认为 `t_buy_amount = 20000`（配置 `kdj_macd_dual_entry.toml`）。
- 清空所有历史数据（`TRUNCATE 6 张 MySQL 表 + rm -rf data/*`），重跑。

### 27. 板块成分股清理
- 从 `data/sector_stocks.json` 移除非沪深主板/创业板的股票（科创板 688/北交所 8/B 股等）。
- 保留规则：`sh60xxxx`（沪主板） + `sz00xxxx`（深主板） + `sz30xxxx`（创业板）。
- 数量变化：**5527 → 4590**，移除 937 只。

### 28. 请求反识别与防拉黑机制（核心增强）
- **随机 UA/Referer 池**：8 款浏览器 UA + 6 个 Referer 来源，每次请求随机组合（`base.py` → `random_headers()`）。
- **限流间隔 2.0s + 0~3s 随机抖动**：可配置 `STRATEGALAB_DATASOURCE_GAP`。
- **10% 概率完全跳过**：让请求节奏进一步稀疏化、不可预测。
- **熔断阈值 8→3 次**：连续 3 次失败即触发冷却，起始冷却 60s，翻倍上限 600s。
- **akshare 适配器切到新浪源**：因东财 IP 被封，`stock_zh_a_hist`(东财) 改为 `stock_zh_a_daily`(新浪)，周线由日线聚合生成。
- **`.env` 默认数据源改为 `akshare`**：彻底绕过被封的东财。

### 文档同步（多数据源 + 风控增强）
- 本文件追加「七、2026-07-19 多数据源抽象层 + 风控增强」章节。
- `docs/需求文档.md`：追加 §7 多数据源需求 + §8 风控与策略需求。
- `docs/技术文档.md`：追加 §16 多数据源扩展 + §17 请求反识别与防拉黑。
- `docs/使用指南.md`：追加 §10.6 数据源切换 + §10.7 服务重启 + §10.8 风险控制。

---

## 八、2026-07-19 网页交互与全市场预热增强（文档同步）

### 29. 分析查询页新增「胜率」列 + 列头可排序 + 修复第 2 页无数据
- **后端 `repository.py` 的 `rank_runs()`**：返回 items 新增 `win_rate_pct` 字段（取自 `Summary.win_rate_pct`，None 存 None）；新增参数 `sort_by: str|None=None` 与 `order: str="desc"`，`sort_by` 白名单 = `symbol_name` / `total_return_pct` / `max_drawdown_pct` / `sharpe` / `win_rate_pct`，非白名单或 None 时保持原默认（按 `total_return_pct` 降序，向后兼容）；`order` 仅接受 `asc`/`desc`；排序在去重后 items 上做，字段值为 None 的统一排到末尾（避免比较报错）。
- **后端 `web.py` 的 `_api_rank()`**：从 query 解析 `sort_by`/`order`（order 非法时回退 desc）并透传给 `rank_runs`；CSV 导出分支同样透传。
- **前端 `web.py` 的 `build_analysis_html()`**：表格新增「胜率」列（位于「夏普」之后、「数据时效」之前，不参与正负着色）；五个列头（股票名称 / 总收益率 / 最大回撤 / 夏普 / 胜率）均可点击排序，当前排序列显示 ▲/▼ 金色高亮，切换后页码重置回第 1 页；重写翻页逻辑修复「第 2 页无数据」——页码守卫、fetch 始终带 `sort_by/order`、空数据守卫、翻页器必渲染、`pages=Math.ceil(total/50)`。
- **验证**：QA 独立离线回归 IS_PASS=YES；后端分页离线 + 线上实测均正常（page=2 返回 50 条），前端逐行静态审查通过。

### 30. 详情页持仓 / 交易明细表头滚动固定（sticky header）
- **文件**：`src/strategylab/engine/vendor/dashboard_template.html`（纯 CSS 调整，未动 JS / HTML 结构 / 后端）。
- **根因**：模板 `thead th` 本已有 `position:sticky; top:0`，但①包裹层 `.trades-table-wrap`（原仅 `overflow-x:auto`）、`.position-table-wrap`（原仅 `overflow-x:hidden`）无 `max-height` 也无 `overflow-y`，sticky 缺少吸附容器；②表头 `background:transparent`，吸附后透穿下方行，表头不固定。
- **修复**：给两个包裹层加 `max-height:62vh` + `overflow-y:auto`（制造纵向滚动容器）；表头 `background` 由 `transparent` 改为 `var(--surface, #161b22)`（不透明，与卡片色协调），新增 `z-index:2` + `box-shadow` 兜底边框。短表（<62vh）无副作用，长表表头吸顶。
- **验证**：QA 静态核验 IS_PASS=YES；`--surface` 在 dark/light 主题均有定义（dark=#161b22），颜色协调无风险。⚠️ 注意：`data/index.html` 是旧产物，需重启服务重渲后新表头才生效。

### 31. 全市场预热市值过滤脚本 + 预热宇宙口径校正
- **新增脚本** `scripts/filter_by_market_cap.py`：可按总市值过滤 `data/sector_stocks.json`，自适应数据源、`ThreadPoolExecutor` 并发、本地续传缓存、防空写保护；可用 `MIN_CAP` / `MAX_CAP` / `WORKERS` 环境变量调参。
- **取消批次功能**：经核验，当前代码「取消不了」问题已不存在——`web.py` 取消按钮已带 `id="cancel-btn"`、`cancelBatch()` 已做 DOM 空值保护、`beginBatch()` 已设 `curBatchId`、后端 `threading.Event` 取消机制完好，QA 实测小批次取消 → 状态变 `cancelled`、轮询停止。本次未做多余代码改动（最小变更）。
- **当前预热宇宙口径**：实际 `data/sector_stocks.json` 仅 **1301 只**（31 个板块、每只 `{code,name}`），经百度源逐只核实全部 1301 只市值 min=100.0 亿 / max=993.01 亿 / 中位数≈193 亿，**已全部落在 100~1000 亿中盘区间**，已符合用户「只保留 100 亿~1000 亿」诉求（按现有文件过滤为「零剔除」）。用户已确认保持 1301 只现状。

文档同步（网页交互与预热增强）
- 本文件追加「八、2026-07-19 网页交互与全市场预热增强」章节（§29 / §30 / §31）。
- `docs/技术文档.md`：§15.3 补充 `/api/rank` 的 `sort_by`/`order` 与 `win_rate_pct` 返回字段；§8 补充详情页表头 sticky；§15.7 分析页补充胜率列与列头排序。
- `docs/需求文档.md`：FR-21 补充胜率列 / 列头排序 / 翻页修复；FR-20 补充 `sort_by`/`order`；FR-08 附近补充详情页表头 sticky；FR-15 的 5500→1301 校正。
- `docs/使用指南.md`：§10.5.2 校正全市场预热数量为 1301 只并补充市值过滤脚本；§10.5.3 补充胜率列 / 列头排序 / 翻页修复；§10.1 补充表头 sticky；两处 5500→1301 校正。

---

## 九、2026-07-19 多策略维护结构重构（M1+M2）+ 体验修复

### 32. 分析查询页空结果「预热中」引导（体验修复）
- **根因**：选策略点「查看详情 / 排名」遇到空结果时，页面只有"暂无已跑过的回测"一句话，无法区分"真没跑"还是"正在跑"，易被误判为 Bug。
- **改动**：`engine/storage/repository.py` 新增 `latest_batch_for_strategy(strategy_name, params_hash=None) -> dict|None`（只读，try/except 兜底）；`web.py` 的 `_api_rank` 在 `total==0` 时附加 `warmup` 信息；前端 `loadRank` 空结果分支按 `warmup.in_progress` 显示"预热仍在进行中（已完成 X/Y）"引导。
- **验证**：QA 独立回归 NoOne；未重启、未触碰运行中进程（改动仅下次重启生效）。

### 33. 海龟策略「查看详情」`KeyError: 'entry'`（Bug 修复）
- **根因**：`engine/dashboard.py` 的 `_note_texts(cfg)` 写死读取 KDJ 双入口策略专用键 `cfg["params"]["entry"]["path_a"]`；海龟配置无 `entry` 键，点详情渲染仪表盘即抛 `KeyError`，被 except 捕获后返回 HTTP 500。
- **修复**：该硬编码分支已由 M1 重构（§34）**结构性消除**；海龟详情现由策略类 `describe()` 自描述，不再依赖任何 KDJ 专用键。
- **验证**：QA 独立回归 35/35 NoOne（含真实海龟 run 详情构建不报错、文案要点齐全）。

### 34. 多策略维护结构重构 M1：展示层去硬编码（策略自描述）
- **目标**：消除"每加一个策略就要在 `dashboard.py` 手写文案分支"的繁琐，根治 §33 类问题。
- **改动**：
  - `engine/strategies/turtle.py` / `kdj_macd_dual_entry.py` 各新增 `@staticmethod describe(params) -> str`，原"策略实现要点"文案原样搬入（内容不变）。
  - `engine/dashboard.py` 的 `_note_texts(cfg)` 改为**接口驱动**：`cls = get_strategy_class(cfg["type"])`；若 `hasattr(cls, "describe")` 则 `note = cls.describe(params)`，否则回退 `_note_texts_generic`；**删除** `_note_texts_turtle` / `_note_texts_kdj` 两函数，以及 `if type=="turtle"…elif…"海龟" in strategy_name` 的类型字符串 + 名称子串兜底分发。
  - 返回结构 `(note, limit, disc)` 三元组不变；通用"已知局限与偏差""免责声明"两段保留。

### 35. 多策略维护结构重构 M2：类注册自动发现
- **目标**：消除"每加一个策略就要手工改 `STRATEGY_REGISTRY` 注册"的繁琐，并根除"配置自动发现 / 类手工注册"双真相源不一致的隐患。
- **改动**：`engine/strategies/__init__.py` 删除手写 `STRATEGY_REGISTRY` 字典，改为 `discover_strategies()` 在导入时扫描本目录 `*.py` 收集 `BaseStrategy` 子类、以 `cls.type` 建表（排除 `base`/抽象/未设 `type`）；`STRATEGY_REGISTRY` 由其构建；`get_strategy_class` / `list_strategies` 签名与返回语义**完全保留**。
- **冲突防御**：两个类 `type` 冲突时导入即抛清晰 `TypeError`（含冲突类型名与两个类名），不静默覆盖。
- **收益量化**：新增策略从「2 新文件 + 2 改中心文件 + ≥2 硬编码分支 + 1 漏注册风险」收敛为「2 新文件、0 改中心文件、0 硬编码分支」。
- **验证**：工程师 IS_PASS: YES；QA 独立回归 35/35 NoOne（含"临时放 demo 策略不碰 `__init__.py` 即被自动发现"、type 冲突清晰报错）。

### 文档同步（多策略结构重构 + 体验修复）
- 本文件追加「九、2026-07-19 多策略维护结构重构（M1+M2）+ 体验修复」章节（§32 / §33 / §34 / §35）。
- `README.md` §3.3「新增一种全新策略逻辑」：移除"在 `__init__.py` 登记注册表"步骤，改为"自动发现、零中心改动"，补充可选 `describe()` 说明。
- `docs/技术文档.md`：§5.1 注册表描述改为自动发现；§9.3 toml `type` 注释改为"唯一、自动发现建表"；§13.2 新增策略步骤同步移除手工登记、补充 `describe()` 与冲突报错提示；模块表 `__init__.py` 一行改为 `discover_strategies()`。
- `docs/strategy-maintenance-review.md`：补充「实施状态」——M1+M2 已于本次会话实施完成（工程师 IS_PASS: YES，QA 独立回归 35/35 NoOne），M3（toml `notes` 纯配置变体）保持待定。
- 配套评审与设计文档：`docs/strategy-maintenance-review.md`（架构评审 + 增量迁移路线 M1→M2→M3）。

---

## 十、2026-07-20 3.0 大更新（拆分预热 + 增量更新数据源）— 已实现（待 QA）（2026-07-20）

> 核心理念：**职责解耦（取数 vs 回测）+ 增量更新（append 而非覆盖）+ 时效可控**。
> 用户诉求：把 v2.0「全市场预热」按钮耦合的「取行情」「跑回测」拆成两个独立按钮；取数从「整文件覆盖、全量重拉」改为「基于缓存 CSV 的增量追加」。
> 铁律：不引入 Redis/Celery；仅用 Python 标准库 + 现有 MySQL/SQLite；复用 v2.0 `batches`/`batch_items` 全套机制。
> 详细设计见 `docs/v3.0-design.md`（含类图 `v3.0-class-diagram.mermaid` 与时序图 `v3.0-sequence-diagram.mermaid`）。

### 36. 拆分"全市场预热"为「更新数据源」+「回测」双按钮（FR-37）
- `/production` 页去掉单一"全市场预热"按钮，改为「更新数据源」与「回测」两个独立操作；两者各自支持三种范围（板块 / 自定义池 / 全市场，全市场由范围单选 `all_market` 提供，不再有独立预热按钮）。
- 点击「更新数据源」不触发任何回测逻辑；点击「回测」不触发任何取数（除非行情缺失报错）。
- 进度区 / 2s 轮询 / 取消 / 板块总览 / 历史批次全部复用 v2.0 机制。

### 37. 更新数据源独立取数、不回测（FR-38）
- 新增 `POST /api/data/update`（data-only 批次）：仅从数据源（默认 akshare 新浪源 `stock_zh_a_daily`）拉取/维护日线+周线行情写 `data/` 缓存 CSV + `<prefix>_<source>_<period>_meta.json`；不跑回测、不落库 `backtest_runs`。
- 复用 `batches`/`batch_items` 表（新增 `batch_type='data'` 字段区分），进度 / 取消 / 板块总览 / 重启-interrupted 全套机制复用，不新建表。

### 38. 增量更新核心（FR-39）
- `KlineCache` 新增 `merge()`（读旧 CSV → 按 `date` 合并去重 `keep='last'` → 排序 → `os.replace` 原子写回，历史 `beg` 不丢）+ `_incremental_window(meta, today, default_beg)`（基于 `meta.last` 计算取数起点 `last+1`，`last>=today` 则跳过）。
- `provider.ensure_data` 新增 `mode='update'`：取数区间从「全量 `[start-1年, 今天]`」改为「`[last+1, 今天]`」，日线/周线各自独立增量；`save()` 由整文件覆盖改为 merge 写回。
- 幂等（重复触发无新交易日则零追加）与续传（中断未写回则 `meta.last` 不变、下次从末日继续）天然成立。

### 39. 回测独立、不复用取数职责（FR-40）
- `ensure_data` 新增 `mode='verify'`：仅校验缓存覆盖所需区间，缺失即抛 `DataMissingError`，回测 `batch_item` 标记 `failed` 并提示"行情缺失，请先点『更新数据源』"，绝不静默全量重拉。
- `backtest.run_symbol` 改为 `ensure_data(mode='verify')`；命中复用（`find_existing_runs`，不比对日期区间）仅用于"回测结果复用"，数据刷新改由独立「更新数据源」负责，结构上解耦。
- 不改 `find_existing_runs`；`POST /api/batch/warmup-all` 保留，语义澄清为"全市场回测"（等同 all_market 回测按钮，不复用取数职责）。

### 40. 数据时效提示增强（FR-41，P1）
- 分析页对 `backtest_runs.end` 距今超过 `STALE_DAYS`（默认 30，可配 `STRATEGALAB_STALE_DAYS`，复用 FR-31 阈值）的 run 标注"数据较旧，建议点「更新数据源」刷新行情后再重跑"。
- 细化/替代 FR-31 文案，明确引导至新增「更新数据源」按钮；`STALE_DAYS` 抽为单一常量，FR-31 与 FR-41 共用。

### 41. 回测清空后全量重生成（FR-42，P0）
- 用户诉求：「回测」按钮拆分出来后，要求先清理该策略之前产生的所有回测信息，再按现有数据源数据重新生成一份。
- 触发语义：`POST /api/batch`（回测按钮）执行时，**先删除该策略（按 `strategy_name + params_hash + data_source` 维度）此前产生的全部 `backtest_runs` 历史结果**（含关联 `equity_points` / `trades` / `summary` / `batch_items` / `batches`），**再基于现有缓存行情（`verify` 模式，绝不重新取数）重新跑一遍所选范围的回测**，生成一份全新的、干净的结果集。
- 清空口径（已决议，按推荐）：按 `strategy_name + params_hash + data_source` **清空该策略全部历史结果（不限所选范围）**；"重新生成的一份"仅覆盖本次所选范围；历史仅保留最新一份（清空后重跑覆盖），不保留 N 份快照。
- 关联表与隔离：`batches` / `batch_items` 为 data/backtest 共用表，清空时**限定 `batch_type='backtest'` 且匹配上述维度**，避免误删「更新数据源」写入的 data 批次；缓存 CSV 完全不受影响。
- **取代 FR-40 的"命中复用跳过"语义**：FR-42 移除回测主链路 `submit_batch` 对 `find_existing_runs` 的命中跳过；**保留 FR-40 的"verify 模式缺数据报错"**（`DataMissingError` → `failed` + "请先点『更新数据源』"）。
- 实现落点：新增 `Repository.clear_strategy_runs(strategy_name, params_hash, data_source)`（事务内顺序 DELETE），`BatchRunner.submit_batch` 在 `create_batch` 之前调用它；详细设计见 `docs/v3.0-design.md` §2（决策 #8 修订 / #11）/ §3.4 / §7.2 / §8（T-42）/ §9 / §11。

### 42. 分析查询列表新增「最近买入日期」列（FR-43，P0）
- 用户诉求：分析查询页（`/analysis`）查询列表新增一列「最近买入日期」，为该列表**每行**对应回测结果产生的 `trades` 中"建仓/买入时间"的**最大值**（最近一次买入日期），并支持正序/倒序排序。
- **字段来源（代码查证）**：trades 表"买入/建仓时间"真实字段名 = `entry_date`（`src/strategylab/engine/storage/schema.py:99`）；trades 仅 A 股多头（`side='long'` 固定，无 buy/sell 之分），故 `last_buy_date = MAX(trades.entry_date)`，**无需 side 过滤**；无 trades 的 run → `NULL` → 前端显示"—"。
- **行粒度（代码查证）**：`rank_runs` 查询单元为 `BacktestRun`，按 `symbol` 去重仅保留最新一条（`repository.py:736-743`），故列表**每行 = 一个 symbol 的最新 run**，与 FR-43 推荐"按每行 run 取各自 trades 最大买入日期"一致，直接采用。
- **取数方式**：在 `repository.rank_runs` 中用**一条分组 MAX 聚合**（`func.max(Trade.entry_date)` 按 `run_id`）算 `last_buy_date` 随行返回，避免逐 run 查 trades 的 N+1；返回 ISO `YYYY-MM-DD` 字符串，无 trades 为 `null`；`summary` 表无建仓字段，故**不冗余存储**，仅随行聚合。
- **排序**：`rank_runs` 的 `sort_by` 白名单（`_ALLOWED_SORT`）新增 `last_buy_date`；升序=正序、降序=倒序，`null` 统一排末尾（沿用 FR-20 的 None 末位规则），切换排序后回第 1 页（沿用 FR-21 翻页守卫）；前端 `build_analysis_html` 列头 `cols` 新增 `['last_buy_date','最近买入日期',true]`，点击在 asc/desc 间切换、金色 ▲/▼ 高亮。
- 实现落点：`engine/storage/repository.py`（`rank_runs` 聚合 + 白名单扩展）+ `web.py`（`build_analysis_html` 新增列与列头排序、空值"—"）；详细设计见 `docs/v3.0-design.md` §3.5 / §5（`/api/rank`）/ §8（T-43）/ §9 / §11。

### 文档同步（3.0）
- 新建 `docs/v3.0-design.md`：3.0 完整增量设计 + 实现计划（升级背景、已确认决策、增量核心、类图、API、前端双按钮、时序图、任务清单、文件清单、依赖、共享知识、风险、待明确事项）。
- 配套 mermaid：`docs/v3.0-class-diagram.mermaid`、`docs/v3.0-sequence-diagram.mermaid`。
- 本文件追加「十、3.0 大更新」章节。
- `docs/需求文档.md` §11：FR-37~41（3.0 增量 PRD，已写好）。
- `docs/使用指南.md` §10.5.2：数据生产页双按钮操作说明 + FR-41 时效提示。
- `docs/技术文档.md`：同步 v3.0 架构扩展（数据模型 / 增量更新 / 双按钮 API / verify 模式）。
