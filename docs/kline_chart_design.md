# 架构设计 + 任务分解：单标的回测明细页 K 线走势图

> 增量功能：在「单标的回测明细页」新增 **股价 K 线走势图（OHLC 蜡烛 + 下方成交量副图 + 蜡烛上叠加策略买卖点标记 + 可缩放交互）**，并**落库**以便历史回测详情也能显示。
> 目标栈：沿用现有栈（纯 Python + 自绘 SVG），**无新依赖**。
> 作者：高见远（架构师） · 团队 software-kline-chart

---

## 0. 结论摘要（给交付总监）

1. **沿用现有栈、无新依赖**：后端纯 Python（SQLAlchemy 2.0 + pandas），前端纯手绘 SVG（与现有 `overview_chart` 同引擎，无 ECharts/Plotly/d3）。HTML 仍自包含。
2. **落库方案选「新增 `PricePoint` 子表」而非给 `backtest_runs` 加列**：已核对 `engine/storage/db.py:80` 的 `init_db()`——先 `Base.metadata.create_all(engine)` 自动建所有**新表**，再跑 `migrate_v1_to_v2 / v2_to_v3 / v3_to_v4`（这些只给**旧表加列**）。新增 `PricePoint`（与 `EquityPoint` 同构、1—* 关联 `BacktestRun`）会被 `create_all` 直接建出，**零迁移代码、风险最低**。给旧表加 `price_json` 列才需要手写迁移函数，方案被否决。
3. **⚠️ 重大发现（超出原文件清单，需你拍板）**：当前系统**完全没有成交量数据**。`daily` DataFrame 经 `data_feed.load_bars` 读出的缓存 CSV 只含 `date,open,high,low,close`（`datasource/cache.py:_atomic_write_csv` 写死 OHLC），四个数据源适配器（eastmoney/akshare/tushare/tencent）也只保留 OHLC。你此前假设「`daily` 含 `vol`」与实际不符。用户已确认需要「成交量副图」，因此**必须**在数据源 + 缓存层补抓成交量。详见 §8。
4. **`web.py` 无需修改**（已核对 `run_backtest` / `_handle_run` / `_render_single_run` / `_api_run_detail`，见 §2、§4）。价格透传只需打通 `run_symbol → save_run → db_row_to_result → build_dashboard_data → 模板` 这条链。

---

## 1. 实现方案 + 框架选型

| 维度 | 选型 | 理由 |
|------|------|------|
| 后端框架 | 沿用 `SQLAlchemy 2.0` + `repository` 仓储层 | 与 `EquityPoint` 完全同构，复用 `bulk_insert_mappings` 批量写、`joinedload` 读、cascade 删 |
| 新增存储 | 新增 `PricePoint` 子表（1—* 关联 `BacktestRun`） | `create_all` 自动建表，无需迁移；字段对齐现有 `Numeric(18,4)/Date` |
| 前端图表 | 手绘 SVG（复用 `overview_chart` 的坐标系/网格/tooltip/标记画法） | 现有模板已是纯 SVG 手绘，新增 `price_chart` 渲染器保持同一套视觉与交互范式，零新依赖 |
| 缩放实现 | 手绘缩放状态机（视窗索引区间 `[i0, i1]`） | 现有图表无缩放，需新增一套基于索引区间的重绘逻辑，纯 JS、无库 |
| 成交量来源 | 数据源适配器补抓 `vol` + 缓存持久化（见 §8） | 用户硬性要求成交量副图；数据源本身具备成交量字段，仅被丢弃 |
| 依赖 | **无新增** | 全部沿用既有栈 |

**为何不引入 ECharts/Plotly**：保持 HTML 自包含、离线可用，且现有 `overview_chart` 已是成熟的手绘 SVG 范例，新增 `price_chart` 直接复用其坐标系与标记画法，一致性最好、回归风险最低。

---

## 2. 文件列表（含新增 / 修改标注）

| 文件 | 变更 | 说明 |
|------|------|------|
| `src/strategylab/engine/storage/schema.py` | **修改** | 新增 `PricePoint` ORM 模型；`BacktestRun` 增加 `price` 关系 |
| `src/strategylab/engine/storage/repository.py` | **修改** | `save_run` 增加 `price_curve` 参数并批量写 `PricePoint`；`db_row_to_result` 读出 `price_curve`；`get_run`/`list_runs_by_ids` 增加 `joinedload(run.price)`；`clear_strategy_runs` 增加删除 `PricePoint` |
| `src/strategylab/engine/storage/serializers.py` | **修改** | `db_row_to_result` 新增 `price_curve` 字段构造 |
| `src/strategylab/engine/backtest.py` | **修改** | `run_symbol` 计算窗口内 `price_curve`（含 volume）并加入返回 dict |
| `src/strategylab/engine/dashboard.py` | **修改** | `build_compare_dashboard` / `build_compare_from_runs` 向 `build_dashboard_data` 透传 `price_curve`（可选：`build_single_dashboard` 同步） |
| `src/strategylab/engine/vendor/render_dashboard.py` | **修改** | `build_dashboard_data` 新增 `price_curve` 形参并存入 `report_data`；`_build_default_modules` 在 `price_curve` 存在时生成 `price_chart` 模块 |
| `src/strategylab/engine/vendor/dashboard_template.html` | **修改** | 新增 `price_chart` 渲染分支（`renderModule` + `buildPriceChart` + 缩放交互） |
| `src/strategylab/engine/datasource/{eastmoney,akshare_src,tushare_src,tencent_src}.py` | **修改（新增前置任务）** | 归一化时补抓 `vol` 列（数据源本就有成交量字段） |
| `src/strategylab/engine/datasource/cache.py` | **修改（新增前置任务）** | `_atomic_write_csv` 增加 `vol` 列；缓存版本号 2 → 3（触发旧缓存刷新） |
| `src/strategylab/web.py` | **无需改（已核对）** | `run_backtest` 仅透传 `run_symbol` 返回 dict；`_render_single_run`/`_handle_run`/`_handle_compare` 走 `build_compare_from_runs`（数据源自 DB）；`_api_run_detail` 直接返回 `get_run`（自动含 `price_curve`）。价格透传不依赖 web.py 改动 |

---

## 3. 数据结构与接口

### 3.1 `PricePoint` ORM 模型（schema.py，新增）

```python
class PricePoint(Base):
    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_runs.run_id"), index=True
    )
    date: Mapped[datetime.date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Numeric(18, 4))
    high: Mapped[float] = mapped_column(Numeric(18, 4))
    low: Mapped[float] = mapped_column(Numeric(18, 4))
    close: Mapped[float] = mapped_column(Numeric(18, 4))
    volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)  # 旧缓存无成交量时为 null
```

`BacktestRun` 新增关系（与 `equity`/`trades` 同范式）：
```python
price = relationship("PricePoint", cascade="all,delete-orphan")
```

### 3.2 `run_symbol` 返回 `result` 新增字段

```python
result["price_curve"] = [
    {
        "date":   "2020-01-02",          # YYYY-MM-DD，仅 [start, end] 窗口内
        "open":   10.50,
        "high":   10.80,
        "low":    10.30,
        "close":  10.70,
        "volume": 1234567.0,             # 来自 daily["vol"]；旧缓存无则为 None
    },
    ...
]
```
构造逻辑（backtest.py，`save_run` 之前）：
```python
window = daily[(daily["date"] >= pd.Timestamp(start)) & (daily["date"] <= pd.Timestamp(end))]
price_curve = [
    {
        "date":   (row["date"].strftime("%Y-%m-%d")
                   if hasattr(row["date"], "strftime") else str(row["date"])[:10]),
        "open":   float(row["open"]),
        "high":   float(row["high"]),
        "low":    float(row["low"]),
        "close":  float(row["close"]),
        "volume": float(row["vol"]) if "vol" in daily.columns else None,
    }
    for _, row in window.iterrows()
]
```

### 3.3 `save_run` 新增参数 + 批量写入（repository.py）

```python
def save_run(
    run_meta, equity_curve, trade_history, summary,
    positions=None,
    price_curve: list[dict] | None = None,   # 新增
) -> str:
    ...
    price_maps: list[dict] = []
    for p in price_curve or []:
        price_maps.append({
            "run_id": run_id,
            "date":   _parse_date(p.get("date")),
            "open":   _safe_float(p.get("open")) or 0.0,
            "high":   _safe_float(p.get("high")) or 0.0,
            "low":    _safe_float(p.get("low")) or 0.0,
            "close":  _safe_float(p.get("close")) or 0.0,
            "volume": _safe_float(p.get("volume")),   # 允许 None
        })
    ...
    with get_session() as s:
        s.add(run)
        s.flush()
        if equity_maps:  s.bulk_insert_mappings(EquityPoint, equity_maps)
        if price_maps:   s.bulk_insert_mappings(PricePoint, price_maps)   # 新增
        if trade_maps:   s.bulk_insert_mappings(Trade, trade_maps)
        s.add(summary_obj)
```

### 3.4 `db_row_to_result` 新增 `price_curve` 读出（serializers.py）

```python
price_curve = [
    {
        "date":   p.date.isoformat(),
        "open":   float(p.open),
        "high":   float(p.high),
        "low":    float(p.low),
        "close":  float(p.close),
        "volume": float(p.volume) if p.volume is not None else None,
    }
    for p in (run.price or [])
]
# 并入返回 dict（与 equity_curve/trade_history 并列）
return {..., "price_curve": price_curve, ...}
```
同时 `get_run` / `list_runs_by_ids` 的 `joinedload` 需增加 `BacktestRun.price`（否则 `run.price` 为 detached 访问报错，与现有 `equity`/`trades` 一致）。

### 3.5 `build_dashboard_data` 新增参数（render_dashboard.py）

```python
def build_dashboard_data(
    ..., 
    price_curve: list[dict] | None = None,   # 新增
    ...
) -> dict[str, Any]:
    ...
    report_data = {
        "meta": meta, "summary": summary,
        "equity_curve": equity_curve, "pnl_curve": ..., "drawdown_curve": ...,
        "trade_history": trade_history,
        "price_curve": price_curve or [],      # 新增：下沉给 _build_default_modules
    }
```

`_build_default_modules` 中追加（在非 event_study 分支）：
```python
price_curve = report_data.get("price_curve") or []
if price_curve:
    modules.append({
        "type": "price_chart",
        "tab": "overview",
        "width": "full",
        "title": "股价走势（K线）",
        "subtitle": f"{meta.get('strategy_name') or 'Strategy'} · 日线 · 含成交量",
        "ohlc": price_curve,                              # [{date,open,high,low,close,volume}, ...]
        "markers": _build_trade_markers(trade_history, market=market),  # 复用现有买卖点
    })
```

### 3.6 `price_chart` 模块 JSON 契约（注入模板）

```json
{
  "type": "price_chart",
  "tab": "overview",
  "title": "股价走势（K线）",
  "subtitle": "xxx · 日线 · 含成交量",
  "ohlc": [
    {"date": "2020-01-02", "open": 10.5, "high": 10.8, "low": 10.3, "close": 10.7, "volume": 1234567.0},
    {"date": "2020-01-03", "open": 10.7, "high": 11.0, "low": 10.6, "close": 10.9, "volume": 980000.0}
  ],
  "markers": [
    {"date": "2020-03-01", "action": "buy",  "label": "短买"},
    {"date": "2020-06-15", "action": "sell", "label": "短卖"}
  ]
}
```

### 3.7 模板 `price_chart` 渲染器输入契约 + 缩放状态机（dashboard_template.html）

**渲染器签名**：`buildPriceChart(host, module, idx)`，宿主容器与 `overview_chart` 一致（`id="module-host-<idx>"`）。

**数据源**：`module.ohlc`（蜡烛 + 量）、`module.markers`（买卖点，复用 `_build_trade_markers` 产出的 `buy/sell`）。

**双面板布局**：上方面板画 K 线蜡烛（约 72% 高），下方副图画成交量柱（约 22% 高），底部留作范围滑块（约 6%）。

**缩放状态机**（纯手绘，无库）：

| 状态 | 含义 |
|------|------|
| `viewRange = [i0, i1]` | 当前可见窗口在 `ohlc` 全量数组上的起止**索引**（含端点） |
| `full = ohlc.length` | 全量点数 |
| `MIN_SPAN = 20` | 最小可见点数（防止缩到单根） |

- **初始化**：`viewRange = [0, full - 1]`；构建 `date→index` 映射一次。
- **滚轮缩放**（绑定 `wheel`）：以光标所在 bar 索引 `ci` 为锚点。`k = deltaY<0 ? 1.15 : 1/1.15`；`span = clamp(round((i1-i0)/k), MIN_SPAN, full)`；`i0 = clamp(ci - round((ci-i0)/k), 0, full-span)`；`i1 = i0 + span - 1`；调用 `redraw()`。
- **拖拽平移**（绑定 `mousedown/mousemove/mouseup`）：`mousedown` 记录 `startX`、`startI0`；`mousemove`（拖拽中）`dx = (e.clientX - startX) / pxPerBar`；`shift = round(dx)`；`i0 = clamp(startI0 + shift, 0, full - span)`；`i1 = i0 + span - 1`；`redraw()`；`mouseup` 结束。
- **范围滑块**（底部 `<input type="range">` 双滑块或两个 range）：`input` 事件直接写入 `viewRange` 并 `redraw()`。
- **重置按钮**：`viewRange = [0, full-1]`；`redraw()`。
- **`redraw()`**：清空 SVG 子节点，仅对 `[i0, i1]` 区间重绘——坐标网格、价格/日期轴、蜡烛实体（收盘≥开盘用 `var(--gain)` 红涨、否则 `var(--loss)` 绿跌，与现有 `--gain/--loss` 一致）、上下影线、成交量柱（颜色同涨跌规则）、落在窗口内的买卖点标记（buy=向上三角 `var(--gain)`、sell=向下三角 `var(--loss)`，复用 `overview_chart` 的标记画法）、tooltip（hover 显示日期/OHLC/量/标记信息）。

**配色 / 标记复用约定**（见 §7）：K 线红涨绿跌沿用 `--gain/--loss`；买卖点三角与 `overview_chart` 的 `LEGEND_DEFS` 完全一致。

---

## 4. 程序调用流程（时序图）

> 见 `docs/kline_chart-sequence.mermaid`（Mermaid `sequenceDiagram`）。
> 要点：路径 A（新回测 `run_symbol → save_run → bulk_insert PricePoint`）与路径 B（历史详情 `get_run → db_row_to_result → joinedload price`）两条数据来源，最终都在 `build_dashboard_data`（生成 `price_chart` 模块）→ `render_dashboard`（注入 `__REPORT_DATA__`）→ 模板 `buildPriceChart` 汇聚。

- **新回测**：`run_backtest`(web.py:85) → `run_symbol`(backtest.py) 计算 `price_curve` 并落库 → 返回带 `price_curve` 的 result → `build_compare_dashboard`(dashboard.py) 透传 `price_curve` 给 `build_dashboard_data`。
- **历史详情**：`/history?run_id=` → `_render_single_run`(web.py:1589) → `build_compare_from_runs([run_id])`(dashboard.py:228) → `list_runs_by_ids` → `db_row_to_result`（读出 `price_curve`）→ `build_dashboard_data`。
- **API**：`/api/runs/<id>` → `_api_run_detail`(web.py:1342) → `get_run`（直接返回含 `price_curve` 的 dict，无需模板）。

---

## 5. 任务列表（有序、含依赖关系、按实现顺序）

> 说明：任务 T1 为**新增前置任务**（成交量补抓），因用户硬性要求成交量副图且当前数据层完全无成交量。若你决定「先不做成交量、仅 K 线+标记+缩放」，可删除 T1，并把 T4 的 volume 置 `None`（副图渲染占位文案），其余不变。
> 任务粒度按功能模块分组，遵循最小变更原则。

### T1 ·【数据层】补抓并持久化成交量 `vol`（前置，阻塞 T4 的 volume 字段）
- **源文件**：`datasource/eastmoney.py`、`datasource/akshare_src.py`、`datasource/tushare_src.py`、`datasource/tencent_src.py`、`datasource/cache.py`
- **依赖**：无（独立前置）
- **优先级**：P0（若要做成交量副图）
- **内容**：
  1. 四个适配器归一化时把各自成交量字段映射到 `vol` 列（eastmoney 解析 raw 串 `f56`；akshare `成交量`；tushare `vol/volume`；tencent `volume/amount`）。缺失时置 `0`/不报错。
  2. `KlineCache._atomic_write_csv` 增加第 6 列 `vol`；缓存 `meta.version` 由 `2` 升到 `3`。
  3. 失效策略（二选一，推荐 a）：**a)** 在 `ensure_data`/`provider` 新鲜度判定中，若缓存 `version < 3` 或 CSV 缺 `vol` 列则视为过期、触发 `update` 模式重抓（一次性刷新旧缓存）；**b)** 仅对新抓取生效，`run_symbol` 用 `if "vol" in daily.columns` 兜底（旧缓存 volume=None，副图显示「成交量数据缺失，请更新数据源后重跑」）。
- **负责模块**：数据层

### T2 ·【存储】`schema.py` 新增 `PricePoint` 模型 + 关系
- **源文件**：`engine/storage/schema.py`
- **依赖**：无
- **优先级**：P0
- **内容**：按 §3.1 新增 `PricePoint` 类；`BacktestRun` 增加 `price = relationship("PricePoint", cascade="all,delete-orphan")`。
- **负责模块**：存储层

### T3 ·【存储】`repository.py` 写/读 `price_curve` + `serializers.py` 读出
- **源文件**：`engine/storage/repository.py`、`engine/storage/serializers.py`
- **依赖**：T2
- **优先级**：P0
- **内容**：
  1. `save_run` 增加 `price_curve` 形参 + `price_maps` 批量写 `PricePoint`（§3.3）。
  2. `get_run` / `list_runs_by_ids` 增加 `joinedload(BacktestRun.price)`。
  3. `clear_strategy_runs` 增加 `s.query(PricePoint).filter(PricePoint.run_id.in_(run_ids)).delete(...)`（与 `EquityPoint` 同处理，保持级联一致）。
  4. `serializers.db_row_to_result` 新增 `price_curve` 构造并并入返回 dict（§3.4）。
- **负责模块**：存储层

### T4 ·【编排】`backtest.run_symbol` 计算 `price_curve` 并透传
- **源文件**：`engine/backtest.py`
- **依赖**：T1（volume 字段）、T3（save_run 签名）
- **优先级**：P0
- **内容**：按 §3.2 过滤 `[start, end]` 窗口，构造 `price_curve`（含 `volume`，旧缓存时 `None`），加入返回 dict 与 `save_run(... price_curve=price_curve)`。
- **负责模块**：回测编排

### T5 ·【构建】`dashboard.py` + `render_dashboard.py` 注入 `price_chart` 模块
- **源文件**：`engine/dashboard.py`、`engine/vendor/render_dashboard.py`
- **依赖**：T4（result 含 price_curve）
- **优先级**：P0
- **内容**：
  1. `render_dashboard.build_dashboard_data` 新增 `price_curve` 形参并存入 `report_data`（§3.5）。
  2. `_build_default_modules` 在 `price_curve` 非空时追加 `price_chart` 模块（`ohlc=price_curve`、`markers=_build_trade_markers(trade_history, market)`）。
  3. `dashboard.build_compare_dashboard` 与 `build_compare_from_runs` 向 `build_dashboard_data` 透传 `price_curve=r.get("price_curve")`。
- **负责模块**：仪表盘构建

### T6 ·【前端】模板 `price_chart` 渲染器（K 线 + 量副图 + 买卖点标记）
- **源文件**：`engine/vendor/dashboard_template.html`
- **依赖**：T5（模块契约生效）
- **优先级**：P0
- **内容**：
  1. `renderModule` 新增 `if (module.type === 'price_chart')` 分支，输出含 `id="module-host-<idx>"` 的卡片（含标题/副标题，可复用 `tv-chart-card` 样式）。
  2. 新增 `buildPriceChart(host, module, idx)`：双面板（K 线 + 量副图）、蜡烛实体/影线（红涨绿跌）、成交量柱、`buy/sell` 标记三角（配色见 §7），网格/坐标轴/tooltip 复用 `overview_chart` 画法。
- **负责模块**：前端模板

### T7 ·【前端】模板缩放交互（滚轮 / 拖拽 / 滑块 / 重置）
- **源文件**：`engine/vendor/dashboard_template.html`
- **依赖**：T6（buildPriceChart 已渲染基础图）
- **优先级**：P1
- **内容**：实现 §3.7 的缩放状态机——`viewRange=[i0,i1]` 索引区间；`wheel` 锚点缩放、`mousedown/move/up` 拖拽平移、底部双滑块范围选择、重置按钮；全部通过 `redraw()` 仅重绘 `[i0,i1]` 窗口。`mountCharts` 中 `if (module.type === 'price_chart')` 分支绑定上述事件。
- **负责模块**：前端模板

### T8 ·【核对】`web.py` 确认无需改动
- **源文件**：`src/strategylab/web.py`
- **依赖**：T4、T5（链路打通后核对）
- **优先级**：P2
- **内容**：已核对 `run_backtest`(85) / `_handle_run`(1651) / `_render_single_run`(1589) / `_handle_compare`(1668) / `_api_run_detail`(1342)。价格经 `run_symbol→save_run→db_row_to_result→build_dashboard_data` 透传，**web.py 不需任何改动**。本任务为回归核对 + 落文档结论。
- **负责模块**：Web 层

### T9 ·【联调】落库 + 历史详情 + 新回测 + 缩放 端到端自检
- **源文件**：（无新增，验证为主）
- **依赖**：T1–T8
- **优先级**：P1
- **内容**：
  1. 新跑一个单标的回测 → 检查 `price_points` 表写入、明细页出现 K 线 + 量 + 标记。
  2. 旧 run（无 price_points）经历史详情打开 → 不报错，`price_curve=[]` 时 `price_chart` 模块不出现（或显示「暂无价格数据」）。
  3. 缩放四件套逐一验证；红涨绿跌 / 买卖点配色与 `overview_chart` 一致。
  4. 删除 run → `price_points` 随 cascade 清除（`clear_strategy_runs` 也覆盖）。
- **负责模块**：全链路

---

## 6. 依赖包列表

**无新增依赖。** 全部沿用既有栈：
- 后端：SQLAlchemy（已用）、pandas（已用）、标准库。
- 前端：纯原生 JS + 手绘 SVG（无 ECharts/Plotly/d3/CDN）。

---

## 7. 共享知识（跨文件约定）

1. **日期格式**：`price_curve` 中 `date` 一律 `YYYY-MM-DD`（ISO），与现有 `equity_curve`/`trade_history` 一致；DB 侧 `PricePoint.date` 为 `Date` 类型，读写经 `_parse_date` / `.isoformat()`。
2. **成交量单位**：与 `daily["vol"]` 一致（即数据源原始成交量口径，通常为「股」；前复权 `qfq` 下价格已复权，成交量保持原始股数）。不做复权换算（如需「复权成交量」列为待明确项，见 §8）。
3. **K 线配色**：红涨绿跌沿用模板现有 CSS 变量 `--gain`（涨/红）、`--loss`（跌/绿），与 `overview_chart` 一致；蜡烛实体 `close >= open` 用 `--gain`，否则 `--loss`。
4. **买卖点标记**：复用 `overview_chart` 的 `LEGEND_DEFS` 画法——`buy`=向上三角 + `var(--gain)`，`sell`=向下三角 + `var(--loss)`；标记数据由 `_build_trade_markers(trade_history, market)` 生成（A 股多头 only，action 仅 `buy/sell`），与权益图标记同源，避免双份逻辑。
5. **窗口对齐**：`price_curve` 仅含 `[start, end]` 窗口（与 `equity_curve`/`trade_history` 同窗口），保证 K 线、权益曲线、买卖点三者日期轴对齐。
6. **空数据兜底**：`price_curve` 为空（`[]`）时，`_build_default_modules` 不追加 `price_chart` 模块，明细页静默不显示该图，不影响其他模块。
7. **存储一致性**：`PricePoint` 与 `EquityPoint` 同属 `BacktestRun` 的 1—* cascade 子表，删除 run / `clear_strategy_runs` 时一并清除。

---

## 8. 待明确事项（含重大发现与建议默认值）

### 8.1 ⚠️ 成交量数据当前完全缺失（最关键）
- **事实**：`daily`（`data_feed.load_bars`）读出的缓存 CSV 仅 `date,open,high,low,close`（`datasource/cache.py:_atomic_write_csv` 写死）；四个适配器只保留 OHLC；**没有任何代码提取或存储成交量**。你此前假设「`daily` 含 `vol`」与实际不符。
- **影响**：用户要求的「成交量副图」无法直接满足——没有 volume 来源。
- **建议默认（已采纳为 T1）**：在数据源 + 缓存层补抓 `vol`（数据源本身具备成交量字段，仅被丢弃），详见 T1。若你选择**暂不补抓**，则 `price_curve.volume` 置 `None`，副图渲染「成交量数据缺失，请更新数据源后重跑」占位，K 线 + 标记 + 缩放仍正常（T2–T9 不受影响）。**请确认走哪条路。**

### 8.2 旧缓存刷新策略
- 升级后旧缓存 CSV 无 `vol` 列。建议：**升级后首次回测触发 `update` 模式重抓**（T1 方案 a），一次性补齐；或接受旧缓存 volume=None（方案 b）。建议默认 a，保证用户立即看到成交量副图。

### 8.3 成交量复权口径
- `daily` 为前复权 `qfq` 价，但成交量通常不被复权（保持原始股数）。是否需要对齐复权因子换算成交量？**建议默认：不换算，直接使用原始 `vol` 股数**（业界常见做法，且 A 股除权除息日成交量本就含权）。如需复权成交量，列为后续增强。

### 8.4 `run_backtest` 是否真经 DB 读取（已核对）
- 已确认：`run_backtest`(web.py:85) 调 `run_symbol`（落库）后走 `build_compare_dashboard`（内存 result），**新回测详情页走 `index.html`**；而 `/history?run_id=` 与 `/api/runs/<id>` 经 `get_run` 从 DB 读。两条路径都通过本设计的 `price_curve` 透传链，结论成立。

### 8.5 多标的对比页是否显示 K 线
- 本设计 `price_chart` 模块 `tab="overview"`，仅在单标的 Tab 渲染。多标的「跨回测对比」Tab 不显示 K 线（避免 N 张图过载），与现有 `overview_chart` 仅在各标的独立 Tab 出现的行为一致。如需对比 Tab 也加，列为后续增强。

### 8.6 字段精度
- `volume` 用 `Numeric(18,4)`，单日成交量（股）远低于 10^14，无溢出风险；若未来接「手」或「百股」单位亦兼容。

---

## 附：交付物清单
- `docs/kline_chart_design.md`（本文）
- `docs/kline_chart-class.mermaid`（类图 / 字段关系）
- `docs/kline_chart-sequence.mermaid`（时序图）
