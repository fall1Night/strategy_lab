# 变更日志 · Strategy Lab

> 截至 **2026-07-19**，按模块归类记录本项目累积的所有功能性改动。
> 详细设计见 `docs/` 下对应文档：使用指南 / 需求文档 / 技术文档 / system_design / src-layout-refactor-design。
> 版本线：v1.0 基线（策略回测引擎 + CLI + 零依赖网页）→ 存储改造 → 包化重构 → Web 交互增强。

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
