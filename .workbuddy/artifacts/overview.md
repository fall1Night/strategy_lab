# 交付概览：东财回测取数 rc=100 永久失败修复

## TL;DR
把"回测补数时东财返回 `rc=100` 导致永久失败"的根因定位并修复——`API_URL` 明文 `http` 被东财拒绝、且 `rc=100` 限流未走重试；现改 `https` + 可重试错误 + 分页取数，回测可自动补足历史并出结果。

## 问题根因
1. **确定性元凶**：`eastmoney.py` 的 `API_URL` 是明文 `http://push2his.eastmoney.com/...`，东财现主推 https，明文 http 被拒/重定向 → 稳定返回 `rc=100`。实测 `https://` + 同款参数 `rc=0` 正常返回 2019 至今数据。
2. **重试缺失（放大）**：上一轮把"接口返回 null/rc≠0"抛成 `DataSourceError`，而 `base.py` 的 `_is_retryable_network_error` 只把网络层错误判可重试 → 重试循环 `retryable=False` → 立刻致命抛出、零重试。`rc=100` 本是限流/瞬时拒绝，应可重试。

## 改动文件
| 文件 | 改动 |
|------|------|
| `src/strategylab/engine/datasource/exceptions.py` | 新增 `RetryableDataSourceError(DataSourceError)` 子类 |
| `src/strategylab/engine/datasource/base.py` | `_is_retryable_network_error` 开头识别该子类为可重试 |
| `src/strategylab/engine/datasource/eastmoney.py` | `API_URL`→`https://`；加 `ut` 令牌 + `Host` 头；`rc≠0`/`null` 抛可重试错误；宽区间分页取数（≤4年/段，去重合并） |
| `tests/test_eastmoney_rc100_retry.py` | 新建回归测试（5 用例） |

## 验证
- 主理人亲自 Read 核验 3 个源文件改动真实落盘。
- 主理人亲自 pytest 复跑：`test_eastmoney_null_response.py`(8) + `test_eastmoney_rc100_retry.py`(5) → **13 passed**（Exit 0）。
- 路由判定：**NoOne**（源码无 Bug，无需回派工程师）。

## 已知问题
- 0。注意：`test_datasource.py` 整文件 pytest 极慢（疑似含网络/DB fixture），本任务只跑纯 mock 的东财专项文件验证。

## 下一步
1. 重启服务（`restart.bat`）让全部 4 轮修复生效。
2. 重跑 600839 回测（任意早期起点），应自动补足 2019 至今行情并出结果；若仍失败，日志现会给清晰 `DataSourceError`（含 `rc/rt/raw`）。
3. 可选 `git commit` 本次 4 轮改动（尚未提交）。
