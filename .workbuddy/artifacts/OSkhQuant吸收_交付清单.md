# OSkhQuant 功能吸收 — 交付清单（strategy_lab）

> 状态：4 项选定功能已全部落地并通过验证；回归已修复。

## 已交付的功能

### 1. 统一交易成本模型 `engine/trading_cost.py`（新）
从 OSkhQuant `khTrade.py` 抽离，**去除 xtquant 依赖**，改为 `TradeCostConfig` + 纯函数，供全部策略共享：
- 佣金（含最低 5 元）、印花税（仅卖方）、过户费（仅沪市）、流量费（每笔固定）
- 滑点（tick / ratio 双模式）、T+0/T+1、整手取整、最大可买量反推
- `buy_cash_out` / `sell_cash_in` 与旧策略 `_fee_cost` / `_fee_proceeds` 默认口径完全一致
- 在策略 `.toml` 的 `[params.trade_cost]` 打开开关即可启用更真实成本，无需改代码

### 2. MyTT 技术指标库 `engine/vendor/myt.py`（新）
OSkhQuant 的 MyTT 原样搬入 vendor 命名空间（仅补 `import math`），60+ 指标（RSI/BOLL/ATR/MACD/KDJ/SAR…），仅 numpy/pandas 依赖，不与 `engine/indicators.py` 冲突。

### 3. A 股交易日历 `engine/trading_calendar.py`（新）
周末排除；`holidays` 库可用时走 `holidays.China()`，否则自动降级为「仅周末」——**零额外依赖**。提供 `is_trade_day` / `get_trade_days_count` / `is_trade_time`。

### 4. RSI / 双均线策略 `engine/strategies/rsi.py` + `dual_ma.py`（新）
- `RSIStrategy`（type=`rsi`）：MyTT `RSI`，超卖(默认30)金叉买入、超买(默认70)死叉卖出，按现金比例建仓，整手、T+1
- `DualMA`（type=`dual_ma`）：MyTT `MA` 短/长双均线，金叉买入、死叉卖出，单底仓
- 配套 `resources/strategies/rsi.toml` 与 `dual_ma.toml`

### 5. 现有 4 策略重构（去重 + 共享成本模型）
`short_buy_sell` / `kdj_macd_cross` / `kdj_macd_dual_entry` / `turtle` 删除各自重复的 `_fee_cost`/`_fee_proceeds`，
统一继承 `base.BaseStrategy` 的共享实现（由 `TradeCostConfig.from_params` 注入）。回测数值不变。

## 回归修复（关键）
`trading_cost.apply_slippage` 原实现在 `ratio=0` 时仍把成交价 `round(price, 2)`，
导致真实收盘价（如 10.0909）被舍成 10.09，使既有策略 pnl 偏移约 23 元
（`test_kdj_macd_cross::test_t1_and_fee_in_pnl` 失败，差 22.76）。
**修复**：滑点无实际调整时返回原始价（不做舍入），仅在有滑点时舍入。已加回归测试锁定。

## 验证结果
- 新增/相关测试 28 项全绿：`test_trading_cost` + `test_trading_calendar` + `test_strategies_smoke` + `test_kdj_macd_cross`
- `dual_ma` / `rsi` 经 `load_strategy_by_arg` 正常加载并自动注册（注册表现已 6 个策略）
- 另 2 个失败在 `test_data_autofill_on_backtest`（datasource 层 ValueError / 日期类型比对），属既有/环境问题，与本吸收无关

## 用户须知 / 限制
- 交易成本默认保持旧口径（无最低佣金、无过户费、无滑点）。要更真实，编辑策略 `.toml` 的 `[params.trade_cost]`。
- 交易日历在 `holidays` 未安装时为「仅周末」近似，法定节假日不剔除；装 `pip install holidays` 即自动升级。
- 运行单个测试（受管 venv，绕过 .env 的 MySQL 依赖）：
  `PYTHONPATH=src ./.venv/Scripts/python.exe -m pytest tests/test_trading_cost.py -o addopts=""`
