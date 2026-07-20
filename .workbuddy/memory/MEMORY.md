# 项目长期记忆（Strategy Lab）

## 协作陷阱（重要）
- **agent 框架偶发"假完成"**：spawn 的子 agent（工程师等）可能回"Task already completed / auto-approved shutdown"，但磁盘文件实际未改动（疑似框架瞬态错误）。**主理人必须在采信前亲自 Grep + Read 核验落盘**，不能轻信 agent 的完成宣告。对策：重派时附带精确 old_string + 强制要求 agent 用 Grep/Read 证明改动已写入。
- **pytest 整文件跑极慢/卡住**：`tests/test_datasource.py` 整文件 pytest 常卡在网络/DB 类 fixture（疑似真实连接），前台易超时转后台且长时间无输出。亲自核验时：**只跑纯 mock 的测试文件（如 test_eastmoney_null_response.py）或用 python -c 单跑关键用例逻辑**，避免整文件 pytest；若要验证被改用例，直接复刻其 mock+断言逻辑用 python 跑，秒级出结果。

## 技术约定
- 数据源切换：`.env` 的 `STRATEGALAB_DATA_SOURCE`（eastmoney/akshare/tushare），failover 到 akshare。磁盘 CSV 在 `data/*_eastmoney_daily/weekly.csv` + `_meta.json`。
- 批次元数据在 **MySQL**(`mysql+pymysql://root@localhost:3306/strategylab`)，本地 `strategy_lab.db` 是误导项（0字节）。
- 回测区间不匹配坑：provider.py / batch_runner.py 的 genesis 起点(20220706)晚于 backtest 要求的 2019，致"行情缺失"失败——尚未修复，待用户授权。
- 受管 Python：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（含 numpy/pandas/pytest）。
