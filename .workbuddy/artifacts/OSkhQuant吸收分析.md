# OSkhQuant → strategy_lab 功能吸收分析

> 分析日期：2026-08-12
> 对象：
> - `OSkhQuant`：GitHub 开源「看海量化回测平台」，PyQt5 + xtquant/miniQMT 架构，纯本地运行
> - `strategy_lab`：你自己的项目，CLI + 网页回测仪表盘，SQLAlchemy 存储，策略自动注册表，东财数据源

---

## 0. 一句话结论

OSkhQuant 的**价值集中在「无 GUI 依赖的纯算法/数据层」**，而它的 PyQt5 界面、调度器、自动更新与你的 Web 仪表盘定位冲突，**不应照搬**。

可吸收优先级（按"缺口大小 × 落地成本"排序）：

| 优先级 | 模块 | 来源文件 | 解决 strategy_lab 的什么短板 |
|---|---|---|---|
| 🔴 高 | 交易成本模型 | `khTrade.py` | 命中 README 自陈短板：无最低佣金/过户费/流量费/滑点/T+0 |
| 🔴 高 | 技术指标库 MyTT | `MyTT.py` | `indicators.py` 仅 MACD+KDJ，缺失 60+ 指标 |
| 🟡 中 | A股交易日历 | `khQTTools.py` | 无交易时间/交易日判断，无法过滤非交易日、算年化 |
| 🟡 中 | RSI / 双均线策略 | `strategies/*.py` | 缺入门级策略类型（双均线、RSI），且 MyTT 引入后正好可用 |
| 🟡 中(可选) | miniQMT 数据源 | `miniQMT_data_parser.py` / `khHistory` | 第二行情源，仅当用户用 QMT 客户端 |
| 🟢 低 | 成分股列表 | `data/*.csv` | 选股宇宙（需架构扩展，非简单吸收） |
| ⚪ 不推荐 | PyQt5 GUI / 调度器 / 自动更新 / 风控 stub / 策略 DSL | 多个 | 定位冲突或空实现，收益低 |

---

## 1. 🔴 交易成本模型（最高价值，命中现有短板）

**来源**：`khTrade.py` 的 `KhTradeManager`（560 行）。

**它有什么**：
- 佣金：`commission_rate` 比例 + **最低 5 元**（`min_commission`）——你现在的策略完全没有这条
- 印花税：`stamp_tax_rate`，**仅卖方**计（`calculate_stamp_tax` 里 `if direction=="sell"`）
- 过户费：`calculate_transfer_fee`，**仅沪市**（`sh.` 开头）收 0.00001
- 流量费：`calculate_flow_fee` 每笔固定（默认 0.1 元）
- 滑点：双模式——`tick` 模式（最小变动价×跳数）和 `ratio` 模式（比例，买上浮/卖下调）
- T+0 / T+1 切换：`set_t0_mode`，影响当日买入是否可卖（`can_use_volume`）
- 最大可买量：`calculate_max_buy_volume` 整手取整 + 扣成本后反推可买股数

**strategy_lab 现状**：每个策略在自己文件里**内联**成本计算。例如 `short_buy_sell.py`：
```python
def _fee_cost(self, size, price):
    return size * price * (1 + self.commission)          # 只有佣金
def _fee_proceeds(self, size, price):
    return size * price * (1 - self.commission - self.stamp_tax)  # 只有佣金+印花税
```
README §7 明确列为局限："佣金按配置比例单边计、未设最低 5 元；印花税仅卖方"。`turtle.py`、`kdj_macd_dual_entry.py` 同理各自手写，口径不统一。

**吸收方式**（建议新增 `engine/trading_cost.py`，抽离成**无 xtquant 依赖**的纯函数模块）：
- 把 `calculate_slippage / calculate_commission / calculate_stamp_tax / calculate_transfer_fee / calculate_flow_fee / calculate_trade_cost / calculate_max_buy_volume` 改成接收 `price/volume/direction/stock_code/cost_config` 的纯函数，去掉 `self.config` 对 KhConfig 的耦合。
- 在 `BaseStrategy` 提供统一成本钩子（如 `self.apply_cost(...)`），4 个现有策略改为调用共享模块，消除重复与口径不一致。
- 滑点、T+0、各费率作为 `.toml` 的 `[params.trade_cost]` 可选字段，默认保持当前简化口径（向后兼容）。

**工作量**：中（抽离依赖 + 4 个策略改造 + 单测）。**价值**：直接消除 README 自陈的所有成本类偏差。

---

## 2. 🔴 技术指标库 MyTT（体量最大、最易直接引入）

**来源**：`MyTT.py`（624 行，来自 mpquant/MyTT，仅依赖 numpy/pandas）。

**它有什么**（60+ 函数）：
- 算子层：`REF / CROSS / HHV / LLV / SMA / DMA / STD / SUM / IF / VALUEWHEN ...`（通达信风格，可直接拼指标）
- 指标层：`MACD / KDJ / RSI / WR / BIAS / BOLL / PSY / CCI / ATR / BBI / DMI / KTN / TRIX / VR / CR / EMV / DPO / BRAR / DFMA / MTM / MASS / ROC / EXPMA / OBV / MFI / ASI / XSII / SAR / TDX_SAR`

**strategy_lab 现状**：`engine/indicators.py` 只有 `compute_macd` + `compute_kdj` 两个函数。

**吸收方式**（推荐"整体引入 + 适配层"，避免重复造轮子）：
- 直接把 `MyTT.py` 放到 `engine/vendor/myt.py`（或 `indicators_mytt.py`），零改动即可 `from ... import MA, RSI, BOLL, ATR`。
- ⚠️ **口径差异需注意**：MyTT 的 `MACD` 返回 `(DIF, DEA, hist×2)` 且用 `EMA`，与你现有 `compute_macd`（返回 DIF/DEA/hist 未×2）口径不同；MyTT 的 `KDJ` 用 `EMA` 平滑（非累计递推）。**不要覆盖**现有 `compute_macd/compute_kdj`，新指标单独命名空间，文档标注差异。
- 也可只抽取你缺的常用几个（RSI/BOLL/ATR/WR/BIAS）补进 `indicators.py` 保持轻量风格——但整体引入性价比更高。

**工作量**：低（复制 + 适配导入）。**价值**：一次性补齐所有常用指标，新策略不用再手写。

---

## 3. 🟡 A股交易日历（基础能力缺口）

**来源**：`khQTTools.py` 的 `is_trade_time() / is_trade_day() / get_trade_days_count()`。

**它有什么**：
- `is_trade_time`：当前是否在交易时段（9:30-11:30 / 13:00-15:00）
- `is_trade_day`：某日是否为交易日（排除周末 + 中国法定节假日，依赖 `holidays` 库的 `holidays.China()`）
- `get_trade_days_count`：区间内交易日天数（年化收益、回测天数的分母）

**strategy_lab 现状**：取数走东财，但回测编排与策略**无任何交易时间/日判断**；年化指标分母、避免非交易日下单、选股截面等场景都缺。

**吸收方式**：新增 `engine/trading_calendar.py`，把这三个函数抽离。节假日判断有两个选择：
- 引入 `holidays` 库依赖（OSkhQuant 已用，pyproject 加一行）；
- 或 strategy_lab 自带一个法定节假日维护表（零额外依赖，但需年度更新）。
供 `backtest.py`（排除未来日期、算交易天数）和策略使用。

**工作量**：低-中（取决于是否加 `holidays` 依赖）。**价值**：补齐基础时间能力，为年化指标/多标的做准备。

---

## 4. 🟡 RSI / 双均线策略（新增两个策略类型）

**来源**：`strategies/RSI策略.py`、`strategies/双均线精简_使用khMA函数.py`。

**逻辑**：
- 双均线：MA5 > MA20 金叉买入、MA5 < MA20 死叉卖出（`khHandlebar` 主函数，约 30 行，极清晰）
- RSI：RSI 阈值超买超卖买卖

**strategy_lab 现状**：已有 `kdj_macd_cross / kdj_macd_dual_entry / short_buy_sell / turtle`，缺"双均线""RSI"这类入门策略。你的策略注册表是**自动发现**（`discover_strategies()` 扫描 `strategies/*.py`），新增零中心改动。

**吸收方式**：在 `engine/strategies/` 下新建 `dual_ma.py`（`type="dual_ma"`）与 `rsi.py`（`type="rsi"`），照搬逻辑 + 复用第 1 项的共享成本模型 + 配 `.toml`。MyTT 引入后指标直接可用。

**工作量**：低。**价值**：丰富策略库，入门示例。

---

## 5. 🟡(可选) miniQMT 数据源

**来源**：`miniQMT_data_parser.py`（1274 行）+ `khQTTools.khHistory/khKline`。

**它有什么**：解析 miniQMT(xtquant) 本地 tick/K 线，行情获取接口。

**吸收方式**：作为 `datasource/qmt/` 第二后端，对接你现有 `datasource.provider` 接口。需要 xtquant 环境（QMT 客户端）。**仅当你实际用 QMT 才值得做**——否则东财源已够用。

---

## 6. 🟢(低) 成分股列表

**来源**：`OSkhQuant/data/*.csv`（沪深300/中证500/上证50/创业板/科创板/全部股票/指数）。

**吸收方式**：作为 `resources/universe/` 资源引入，提供可选"股票池"加载器。但 README 明确 strategy_lab 当前是**单标的回测，不含选股截面**，要做多标的组合需较大架构扩展——短期可作为数据资源保留，不建议现在就铺开。

---

## 7. ⚪ 不推荐搬的部分（及原因）

| 模块 | 原因 |
|---|---|
| PyQt5 GUI（GUI.py / GUIkhQuant.py / GUIDataViewer / backtest_result_window 等） | 与你 Web 仪表盘定位冲突；4700+ 行强绑定 PyQt5，搬运成本极高收益低 |
| GUIScheduler / update_manager / version | 自动更新+调度器，你已有 Web 服务；version 简单无需搬 |
| khRisk.py | 风控方法全是 stub（`return True`），仅字段命名可作模板，无实现价值 |
| khQuantImport 的策略 DSL（StrategyContext/khGet/khPrice/khHas） | 强绑定 OSkhQuant 的 data dict 格式；你现有 `BaseStrategy.run(daily, weekly)` 用 pandas 更直接解耦，搬来会割裂架构 |
| 数据下载器（download_and_store_data 等） | 你 `datasource` 已现代覆盖，东财取数更完整 |

---

## 8. 建议落地顺序

1. **第 1 步（必做，最高性价比）**：引入 MyTT 指标库（低风险、立即可用）+ 抽离交易成本模型（命中短板）。
2. **第 2 步**：交易日历（补齐基础能力）。
3. **第 3 步**：用新成本模型 + MyTT 改写现有 4 策略的 fee 逻辑，并新增 dual_ma / rsi 策略。
4. **（按需）**：miniQMT 数据源 / 成分股股票池。

> ⚠️ 以上内容由 AI 基于两项目源码分析整理，仅供参考，不构成任何投资建议。
