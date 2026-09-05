# 项目长期记忆（Strategy Lab）

## 协作陷阱（重要）
- **agent 框架偶发"假完成"**：spawn 的子 agent（工程师等）可能回"Task already completed / auto-approved shutdown"，但磁盘文件实际未改动（疑似框架瞬态错误）。**主理人必须在采信前亲自 Grep + Read 核验落盘**，不能轻信 agent 的完成宣告。对策：重派时附带精确 old_string + 强制要求 agent 用 Grep/Read 证明改动已写入。
- **pytest 整文件跑极慢/卡住**：`tests/test_datasource.py` 整文件 pytest 常卡在网络/DB 类 fixture（疑似真实连接），前台易超时转后台且长时间无输出。亲自核验时：**只跑纯 mock 的测试文件（如 test_eastmoney_null_response.py）或用 python -c 单跑关键用例逻辑**，避免整文件 pytest；若要验证被改用例，直接复刻其 mock+断言逻辑用 python 跑，秒级出结果。
- **TeamCreate 工具不可用**：本项目的软件团队 SOP 协作中，`TeamCreate`/`TeamDelete` 调用报 "Tool Not Found"。改用直接 `Agent` 派工（`name`=`subagent_type`=`software-engineer` / `software-qa-engineer` / `software-product-manager` / `software-architect`），实质 SOP 流转（主理人中转、成员独立产出、主理人亲自核验落盘）不变。

## 技术约定
- 数据源切换：`.env` 的 `STRATEGALAB_DATA_SOURCE`（eastmoney/akshare/tushare/broker）。**"新浪数据源"= `akshare` 适配器**（底层 `stock_zh_a_daily`），代码里没有独立 `sina` 源名；因东财 IP 限流，当前默认 `akshare`。磁盘 CSV 按源前缀区分：`data/*_akshare_daily/weekly.csv` + `_meta.json`（标 `source: akshare`），旧 eastmoney 缓存为 `*_eastmoney_*`。
- 批次元数据在 **MySQL**(`mysql+pymysql://root@localhost:3306/strategylab`)，本地 `strategy_lab.db` 是误导项（0字节）。
- 回测区间不匹配坑：provider.py / batch_runner.py 的 genesis 起点(20220706)晚于 backtest 要求的 2019。已通过「双向补 + 回测自动补足」缓解；切到 akshare 后其 `stock_zh_a_daily` 直接返回 2019+ 全量历史，该坑实际已不再触发（genesis 常量本身未改，但不再导致"行情缺失"）。
- 受管 Python（关键）：**必须用 venv** `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（同时装了 `strategylab`(editable, 指向 src) + `akshare 1.18.64` + numpy/pandas/pytest）。基础解释器 `versions/3.13.12/python.exe` **缺 strategylab 模块**，运行/服务/测试用它会 ModuleNotFoundError。
- **策略注册表真实位置（重要）**：新增策略子类后，**自动注册靠 `src/strategylab/engine/strategies/__init__.py` 的 `discover_strategies()`**（扫描 `engine/strategies/*.py` 收集 `BaseStrategy` 子类，键为 `cls.type`），CLI 用 `get_strategy_class(cfg["type"])(cfg)` 实例化并 `strat.run(daily, weekly, start, end, symbol=, symbol_name=)`。`engine/config.py` **没有**注册表（仅有 `load_strategy*` / `list_available_strategies`），不要往那里找 `STRATEGY_REGISTRY`。⚠️ 已知 Read 工具曾返回一份"含 discover_strategies 的 config.py"内容与磁盘实际不符——凡是涉及注册/派发，一律以 `engine/strategies/__init__.py` 与 `backtest.py(run_symbol)` 为准，导入前先 Grep 确认真实符号存在。
- **回测"已更新数据源却仍联网"根因（FR-55 已修复）**：旧行为：`cache._covers(meta,beg,end)` 要求 `meta.end >= end`。快速回测表单 end 默认填"今天"，缓存末日永远是最后交易日（<今天），导致每次 verify 都失败 → DataMissingError → update 模式联网重拉。**FR-55 修复**：`_covers` 新增 `end_tolerance_days` 参数（默认0保持兼容），`_ensure_verify` 使用 `VERIFY_END_TOLERANCE_DAYS=7` 容忍末日滞后。修改仅影响 verify 路径（快速回测），数据生产端（update 模式）不动。
- **FR-55 补充——新股首日容差**：末日容差修了之后，2026年上市的新股（如301531）仍重复拉取。根因是 `_covers` 的 `mb <= beg` 检查对新股永远为 False（缓存首日=IPO日 > GENESIS=20200101），end 容差没机会执行。修复：`provider.py` 新增 `_end_within_tolerance()` 静态方法，`_ensure_verify` 中 `_covers` 失败后若 meta 存在则回退为仅检查末日容差——数据源本来就没有 IPO 前的数据，只要末日足够新即视为可用。
- **多键组合排序正确姿势（重要）**：`repository.rank_runs` 做多键稳定排序时，必须从**最低优先级往最高优先级**排（循环 `reversed(_rules)`），靠 Python 稳定排序保住高优先级键顺序，次级键仅在"高优先级相等"时破平。**禁止**按优先级正序排（会让次级键主导全局）。且排序前须先取出"主键缺失"行（`null_primary=[x for x in items if x.get(primary_key) is None]`）保留查询序、最后 `items=head+tail+_null_primary` 附加，否则缺失行会被次级键重排（违背 FR-20/FR-43"主键缺失行保持查询序"设计）。白名单 `_ALLOWED_SORT` 含 `last_buy_date` 等；最多取前 3 键。

## 金融数据工具（westock-mcp / tdx-connector）
- **westock-mcp `tool_filter` 后端不可用**：`tool_filter`（高级选股/preset）调用稳定返回 `高级选股异常：error_type=2 msg=service error`，疑似该接口故障。**替代方案**：用 `tool_ranking`（metric=CompScore/fin_profit/fin_growth/fin_valuation/PE 等，支持 `universe` 板块码）做量化筛选，结论不受影响。
- **板块码前缀规则**：`data_sector` 取成分股/子行业时，申万一级用 `sw1_pt01801050`（有色金属），申万二级必须带 `sw2_` 前缀（如 `sw2_pt01801053` 贵金属）；裸码 `pt01801053` 会报 service error。
- **有效排行指标（tool_ranking metric）**：CompScore/FunmScore/RiskScore/TecScore/CapScore（评分组）；fin_profit(盈利/RoeTTM)/fin_growth(成长/营收增速)/fin_valuation(估值/PE_TTM)/fin_cash_size/fin_liquidity/fin_operation/fin_pershare（财务排行组）。`PE`/`DividendYield` 不是合法 metric 名。
- `data_quote` 支持 `codes` 逗号批量，返回现价/pe_ratio/pe_fwd/pb_ratio/dividend_ratio_ttm/total_market_cap/high_52week/low_52week/chg_ytd 等，可直接锚定目标价。

## 策略落地与回测自检流程（trend_pullback 经验沉淀）
- **新增策略 SOP（按现有框架）**：①在 `src/strategylab/engine/strategies/<name>.py` 写 `BaseStrategy` 子类并设 `type`，无需手写注册（`discover_strategies()` 自动扫）；②`src/strategylab/resources/strategies/<name>.toml` 放参数（toml 注释支持好）；③docs/ 写规则说明，把口语逐条映射为量化条件并标注"量化假设/直接可译"；④产出三件套走项目自带 `vendor.export_results(write_files=True, output_dir=...)` 写本地 CSV，**不要**依赖 `run_symbol`（它要联网 ensure_data + MySQL 落库，验证脚本自加载本地 akshare CSV 更稳）。
- **期末强平必须在评估窗口内**：**教训**——原 `trend_pullback.py` 期末强平取 `daily.iloc[-1]`，当数据末日 > eval_end 时会把成交记到窗口外、被 export slice 误删。修复：从右往左找 `eval_start <= ts <= eval_end and not pd.isna(close)` 的最后一根 bar 强平。**适用所有策略**。
- **描述方法 `describe(params)` 静态**：项目现有惯例，UI 用它呈现策略摘要，注意 `pf/mj/en/ex` 都需从 params 兜底 `dict.get(...) or {}`，否则 NameError。
- **自检 4 步硬规则**（项目里几乎没有现成测试，靠写"_"开头临时脚本验证）：①`grep "shift(-" "iloc\[i+"` 必须空（防未来函数）；②`merge_asof direction="backward"`（不是 forward，否则泄露未收盘周线）；③交易明细手核 PnL：用 `trading_cost.buy_cash_out`/`sell_cash_in` 重算，对得上；④buy&hold 对照差距 >10x 必查原因（持仓/口径/数据完整性）。
- **回测—buy&hold 跑赢 ≠ bug**：低吸/趋势策略天然吃不满涨幅，主要看是否**控回撤**与**纪律性**。判读时给"策略吃不满主升 = 它承诺的不做下降趋势反弹"作为价值，但要诚实说择时 alpha 可能是负。
- **本地 akshare 缓存命名**：`<code>_<sh|sz>_akshare_daily.csv` + `<code>_<sh|sz>_akshare_weekly.csv`（**不是** `<code>_daily.csv` 的统一格式）。akshare 数据从 2020-01 起，前复权 qfq，已含 `vol`（成交量，未含 amount）。
- **项目 vendor 与 expert reference 同构**：`src/strategylab/engine/vendor/{export_results.py, render_dashboard.py, dashboard_template.html, dashboard_locales.py}` 即 expert `reference/` 同名物——回测/仪表盘可直接 `from strategylab.engine.vendor import render_dashboard as rd` 复用，无需自造 HTML。多标的口径：每个标的跑 export 产出独立三件套 → 自建 `combo_equity` 等权 → 调 `rd.build_dashboard_data(equity_curve=combo, summary=..., meta=..., language="zh")` 拿合规骨架 → **`report_data["modules"] = 自定义模块列表`** 整体替换（避免默认 trades_table 空表 + markers 乱画）。
- **headless Chrome 截图自检（Windows）**：`chrome.exe --headless=new --disable-gpu --no-sandbox --hide-scrollbars --window-size=1680,5200 --user-data-dir=<profile> --virtual-time-budget=20000 --screenshot=<abs path> file:///<html>`。**关键**：`--screenshot` 必须给**绝对 Windows 路径**，相对路径会被无声丢弃；`--user-data-dir` 必须存在否则报 profile 错；`--virtual-time-budget` 给足让图表（Chart.js）渲染完。渲染完 `--user-data-dir` 目录可删（临时）。
