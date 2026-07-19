# 策略回测程序（Strategy Lab）

把已验证的量化策略**保存成可复用程序**：输入不同标的与日期，一键生成回测仪表盘。
支持**维护多个策略**（每个策略 = 一个 `.toml` 配置文件），并提供**零依赖的网页回测服务**（浏览器里联网实时搜索标的、选日期、选策略）。

> 配套文档：
> - 产品需求：[docs/需求文档.md](docs/需求文档.md)
> - 技术设计：[docs/技术文档.md](docs/技术文档.md)
> - **使用指南（手把手，零基础也能跑）**：[docs/使用指南.md](docs/使用指南.md)

---

## 0. 快速上手（30 秒）

1. 装 Python 3.11+（勾选 Add to PATH）→ `pip install -e .`
2. 跑回测：`strategylab --symbols 600216.sh --start 2023-01-01 --end 2024-12-31`
3. 开网页：`strategylab-web` → 浏览器开 http://127.0.0.1:8000

**完全不懂编程？** 直接看 [使用指南.md](docs/使用指南.md)，每一步都能复制粘贴。

---

## 1. 安装与快速开始

```bash
cd strategy_lab
pip install -e .          # 以可编辑模式安装（生成 strategylab / strategylab-web 命令）
```

### 1.1 命令行（CLI）

```bash
# 单标的
strategylab --symbols 002001.SZ --start 2023-07-18 --end 2026-07-18

# 多标的对比
strategylab --symbols 600216.SH 300765.SZ --start 2023-07-18 --end 2026-07-18

# 指定展示名称（仅展示用）
strategylab --symbols 002001.SZ --names 新和成 --start 2023-07-18 --end 2026-07-18

# 列出可用策略（内置 + STRATEGALAB_STRATEGIES_DIR）
strategylab --list-strategies

# 列出数据库中已保存的回测历史
strategylab --list-runs
```

等价地，也可以用 `python -m strategylab ...` 运行（无需安装即可，需 `pip install -e .` 装好依赖）。

运行后在 `data/` 目录（默认，由 `STRATEGALAB_DATA_DIR` 控制）生成 `index.html`（双击用浏览器打开即可看完整仪表盘）。
**单标的与多标的统一走「策略对比」排版**：Tab1 为「策略对比」（多权益曲线 + 指标对比表），其后每个标的各占一个独立 Tab，含权益图、指标表、合并持仓成交明细表与策略说明。

### 1.2 网页服务（推荐）

```bash
strategylab-web            # 默认 http://127.0.0.1:8000
# 或指定端口： PORT=9000 strategylab-web
```

浏览器打开 `http://127.0.0.1:8000`：

1. **标的**：输入框支持**联网实时模糊搜索**——输入名称或代码（如「浙江」「600216」）即弹出下拉候选，点击填入；支持逗号/空格分隔多选。
2. **开始 / 结束日期**：评估区间 `YYYY-MM-DD`。
3. **策略**：下拉框列出「包内置策略 + `STRATEGALAB_STRATEGIES_DIR` 目录」下所有 `.toml`。
4. **运行回测** → 页面直接渲染「策略对比」排版仪表盘（与命令行一致）；点「← 新建回测」可返回表单重跑。
5. **回测历史 / 跨回测对比**：打开 `http://127.0.0.1:8000/history` 查看已落库的回测列表，勾选多个即可一键「跨回测对比」（权益曲线按各 run 起点 rebased 到 100 叠加，指标表保留原始 %）。

> 服务仅用 Python 标准库（`http.server`），**零额外依赖**，离线可用（行情已缓存时无需联网）。回测结果统一落库（见 §6），不再写三件套数据文件。

### 1.3 参数说明

| 参数 | 必填 | 说明 |
|---|---|---|
| `--symbols` | ✅ | 标的代码，可多个。支持 `002001.SZ` / `sh600216` / `600216.SH` 等写法 |
| `--start` | ✅ | 评估开始日 `YYYY-MM-DD` |
| `--end` | ✅ | 评估结束日 `YYYY-MM-DD` |
| `--strategy` | | 策略配置文件路径 或 别名，默认 `kdj_macd_dual_entry`（内置） |
| `--names` | | 与 `--symbols` 一一对应的展示名称（可选） |
| `--out` | | 输出目录，默认 `data/` 目录（由 `STRATEGALAB_DATA_DIR` 控制） |
| `--list-strategies` | | 列出可用策略（内置 + 用户目录） |
| `--list-runs` | | 列出数据库中已保存的回测历史 |

---

## 2. 默认策略：「周线MACD + 日线KDJ 双入口做T」（`kdj_macd_dual_entry`）

内置配置文件：`src/strategylab/resources/strategies/kdj_macd_dual_entry.toml`（随包分发，用 `importlib.resources` 读取）。

**建仓（两条平行入口，同一时间仅持一笔底仓）**
- 路径A：周线 MACD 柱(hist)<0 进入监控区；当某一周 hist 较上周上涨（动能筑底转强）→ 日线 J<50 尾盘买入底仓；hist 回到 0 轴上方自动解除监控。
- 路径B：周线 hist<0 且 周 J<30 且 本周周 J 较上周上涨 → 直接尾盘买入底仓（不卡日线 J）。

**做T（仅看日线 J 线）**
- 加仓：J 较近 5 日高点回落 ≥30 且仍在下降（J<前日J）→ 买 1 万。
- 停止加仓：J 反弹（J≥前日J）→ 不买。
- 卖出加仓：J>80 → 卖光全部加仓部分，底仓不动。

**清仓**：日线 MACD 水上死叉（DIF>0、DEA>0、DIF 下穿 DEA）→ 当日收盘清仓全部。

> 执行价 = 当日信号 + 当日收盘（含 look-ahead 偏差，已在策略中显式声明）。

---

## 3. 如何维护 / 新增策略

### 改参数（最常见）
直接编辑策略 `.toml` 的 `[params]`（底仓金额、做T金额、J 阈值、MACD/KDJ 周期等），重新运行即可，**无需改代码**。
内置策略在包内（不要直接改包内文件），复制一份到 `STRATEGALAB_STRATEGIES_DIR` 目录改参数即可覆盖。

### 新增一条同类策略（自定义 toml）
复制内置 `kdj_macd_dual_entry.toml` 改名字与参数，例如放到 `STRATEGALAB_STRATEGIES_DIR/my_variant.toml`，然后：
```bash
strategylab --strategy my_variant --symbols 002001.SZ --start 2023-07-18 --end 2026-07-18
# 或直接给文件路径：
strategylab --strategy /path/to/my_variant.toml --symbols 002001.SZ --start 2023-07-18 --end 2026-07-18
```

### 新增一种全新策略逻辑
1. 在 `src/strategylab/engine/strategies/` 下新建一个 `.py` 文件，定义继承 `BaseStrategy` 的子类，设置类属性 `type`，实现 `run(daily, weekly, start, end, symbol, symbol_name)` 返回 `{"equity_curve","trade_history","positions"}`。
2. （可选）在策略类上实现 `@staticmethod describe(params) -> str`，返回「策略实现要点」文案——它会在分析查询的详情页仪表盘里展示。不实现则自动回退到通用文案。
3. 写一份对应的 `.toml`（`type` 字段与类名一致），放到内置 `resources/strategies/` 或 `STRATEGALAB_STRATEGIES_DIR`。

> ✅ **无需再手工登记注册表**：`engine/strategies/__init__.py` 现通过 `discover_strategies()` 在导入时自动扫描本目录所有 `BaseStrategy` 子类、并以 `cls.type` 建表；新策略只要类文件存在、设了 `type`，即被自动发现，**零中心文件改动**。若两个类 `type` 冲突，导入时会抛清晰的 `TypeError` 提示（含冲突类型名与两个类名）。

---

## 4. 目录结构（src 布局）

```
strategy_lab/
├── pyproject.toml          # 包配置 + console_scripts（strategylab / strategylab-web）
├── README.md
├── LICENSE                 # MIT
├── .env.example            # DATABASE_URL / STRATEGALAB_DATA_DIR / STRATEGALAB_STRATEGIES_DIR 示例
├── src/
│   └── strategylab/
│       ├── __init__.py     # 公共 API（import 即 load_dotenv + setup_logging）
│       ├── __main__.py     # python -m strategylab → cli.main()
│       ├── cli.py          # CLI 入口（strategylab）
│       ├── web.py          # Web 服务入口（strategylab-web）
│       ├── settings.py     # load_dotenv + get_data_dir / get_strategies_dir
│       ├── logging_config.py
│       ├── engine/
│       │   ├── __init__.py # re-export 常用 engine API
│       │   ├── config.py   # load_strategy / load_strategy_by_arg / list_available_strategies
│       │   ├── indicators.py
│       │   ├── data_feed.py
│       │   ├── search.py
│       │   ├── backtest.py
│       │   ├── dashboard.py
│       │   ├── storage/    # 回测结果存储层（DB 无关）
│       │   ├── strategies/ # 策略实现 + 注册表
│       │   └── vendor/     # 自带仪表盘渲染资源（render_dashboard / dashboard_template.html / ...）
│       └── resources/
│           └── strategies/ # 内置默认策略 toml（importlib.resources 读取）
├── tests/
│   ├── conftest.py
│   └── test_storage.py     # 存储层单元测试（SQLite 内存库）
├── data/                   # 运行时生成：行情缓存 CSV + 渲染视图 HTML（gitignored）
├── docs/                   # 需求文档 / 技术文档
└── .env                    # 本地环境变量（不入库）
```

---

## 5. 数据库存储配置（DATABASE_URL）

回测结果（权益曲线 / 成交 / 指标 / 持仓）统一落库，**不再写三件套数据文件**。存储层基于 SQLAlchemy 2.0，**DB 无关**，仅靠环境变量 `DATABASE_URL` 切换：

| 数据库 | DATABASE_URL 示例 | 额外依赖 |
|---|---|---|
| SQLite（默认，零依赖） | `sqlite:///./strategy_lab.db` | 无（仅靠标准库） |
| MySQL | `mysql+pymysql://user:pass@host:3306/strategylab` | `pip install pymysql` |
| PostgreSQL | `postgresql+psycopg2://user:pass@host:5432/strategylab` | `pip install psycopg2-binary` |

- 未设置 `DATABASE_URL` 时默认 `sqlite:///./strategy_lab.db`，本地零额外安装即可跑通。
- 切换数据库只需改 `DATABASE_URL`，**代码无需改动**（已用 `String` / `Numeric` / `Date` / `DateTime` 等跨库类型，避免原生 `UUID` / `JSON` 列）。
- `strategylab.engine.storage` 提供：`db.py`（连接/会话单例）、`schema.py`（4 张表 + 版本表）、`repository.py`（CRUD）、`serializers.py`（行→dict）、`migrate.py`（`init_db` + `schema_version`）。
- 数据库不可用时回测会**明确报错并终止**（不再静默回退文件）。

### 回测历史与跨回测对比（Web）

- `GET /history`：历史页（多选列表）。
- `GET /api/runs?symbol=&strategy=&limit=`：轻量摘要列表（JSON）。
- `GET /api/runs/<id>`：单个 run 完整数据（JSON）。
- `POST /compare {run_ids:[...]}`：跨回测对比仪表盘（自包含 HTML，权益曲线 rebased 到 100）。
- CLI：`strategylab --list-runs` 打印数据库中的回测历史。

---

## 6. 环境注意事项

- **行情取数**：日/周线来自东方财富 `push2his.eastmoney.com`（本环境可达），前复权 `qfq`，首次运行联网取数并缓存到 `data/` 目录下的 `<prefix>_daily.csv` / `<prefix>_weekly.csv`，重复运行直接复用。
- **标的搜索**：全 A 股列表接口改用 `datacenter-web.eastmoney.com`；首次搜索联网拉取并缓存 10 分钟，之后本地按名称/代码过滤。
- **依赖**：`pandas` / `numpy`（回测与指标所需）+ `sqlalchemy` / `pymysql` / `python-dotenv`（存储与配置）。Web 服务与仪表盘渲染仅用 Python 标准库，**无需额外安装**。

---

## 7. 局限与说明

- 当日收盘执行存在 look-ahead 偏差（实盘需次日开盘）。
- 涨跌停（±10%）未建模；佣金按配置比例单边计、未设最低 5 元；印花税仅卖方。
- 清仓信号当日强制平仓所有仓位（含当日 T 加仓），严格 T+1 下当日买入份额本无法卖出，本程序按清仓价强制处理（极少出现）。
- 单标的回测，不含选股截面 / 存活偏差讨论。
- 回测结果由模型/程序驱动，**不构成任何投资建议**。

> ⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。
