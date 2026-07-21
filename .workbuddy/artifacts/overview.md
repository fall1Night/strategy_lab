# 切换数据源到新浪（akshare 适配器）— 交付概览

## TL;DR
把默认数据源从东财（eastmoney）切到新浪（akshare 适配器），绕开东财 IP 限流；已端到端验证 600839 经新浪成功拉取 2019+ 全量日线，Web 服务已重启上线。

## 改动
- `.env` 第 3 行：`STRATEGALAB_DATA_SOURCE=eastmoney` → `akshare`（注释同步更新）

## 关键事实（避免再踩坑）
- 代码里**没有独立 `sina` 源名**（factory 仅注册 eastmoney/akshare/tushare/broker）。用户说的"原有的新浪数据源"= `akshare` 适配器（`akshare_src.py`），其底层调新浪 `stock_zh_a_daily`。所以切换就是改 `STRATEGALAB_DATA_SOURCE=akshare`。
- `.env` 经 `strategylab/__init__.py`→`settings.py:load_dotenv()` 在 import 时灌入环境变量，服务重启即生效，无需改代码。
- **必须用 venv 解释器** `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（同时含 `strategylab`+`akshare`）；基础解释器 `versions/3.13.12` 缺 `strategylab`，用了会 ModuleNotFoundError。

## 验证证据（主理人亲自执行）
- `DataSourceConfig.from_env().default_source == "akshare"`；`factory.get_effective_source("600839.SH") == "akshare"`
- 直连 `fetch_kline` 经新浪返回 132 行真实日线 OHLC
- Web `POST /api/data/update`（600839）批次：`status=done, total=1, done=1, failed=0`
- 缓存落地：`data/600839_sh_akshare_daily.csv` **1830 行**（2019-01-02→2026-07-21），`_meta.json` 标 `source: akshare`
- Web 服务 `http://localhost:8000/` → HTTP 200

## 状态
服务已重启运行，可直接在网页回测 / 点「更新数据源」。用户原"任何日期回测都行情缺失"的根因已用新浪源彻底绕开。

## 回退
东财限流冷却后如需切回：改 `.env` `STRATEGALAB_DATA_SOURCE=eastmoney` 并重启服务即可。

## 待办
4 轮 eastmoney 代码修复 + 本次切换仍未 git 提交（用户未最终确认）。
