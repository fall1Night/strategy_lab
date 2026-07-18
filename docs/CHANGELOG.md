# 变更日志 · Strategy Lab

> 截至 **2026-07-19**，按模块归类记录本项目累积的所有功能性改动。
> 详细设计见 `docs/` 下对应文档：使用指南 / 需求文档 / 技术文档 / system_design / src-layout-refactor-design。
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
- 本文件追加「六、v2.0 大更新」章节。
