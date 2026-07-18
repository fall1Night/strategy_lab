# Strategy Lab · 数据库存储改造架构设计（v1 设计稿）

> 作者：高见远（架构师）｜ 面向：主理人齐活林 + 工程师
> 目标：把"回测结果落盘"从「按标的命名的 CSV/JSON 三件套」改造为「DB 存储（SQLAlchemy，DB 无关）」，
> 并打通**跨回测比对**这一核心能力缺口。回测引擎逻辑（策略 `.run()`、指标、取数）**不改**。

---

## 0. 实际落地与本文差异（v2 更新）

本文为初版设计稿，以下为最终落地时的关键决策与差异，阅读时以本注为准：

1. **落库策略：仅落库，不双写文件。** 用户最终决策：回测结果**只进数据库**，弃用 equity/trades/summary 三件套文件；DB 不可用时**明确报错并终止**（`run.py` → `cli.py` 中 `raise SystemExit`），不再静默回退文件。
2. **包结构：已迁入 `src/strategylab/`。** 本文出现的 `engine/`、`run.py`、`web_app.py` 现已分别对应 `src/strategylab/engine/`、`src/strategylab/cli.py`（命令 `strategylab` / `python -m strategylab`）、`src/strategylab/web.py`（命令 `strategylab-web`）；策略 `.toml` 内置在 `src/strategylab/resources/strategies/`（用户自定义走 `STRATEGALAB_STRATEGIES_DIR` 或 `--strategy <路径>`）。
3. **新增网页端点**：`GET /history`、`GET /api/runs`、`GET /api/runs/<id>`、`POST /compare`（跨回测对比，权益曲线各 run 起点归一化到 100）。
4. 存储层文件路径现为 `src/strategylab/engine/storage/*`，对外 API 与本文一致。
5. 本文 §8 的待确认项 1（双写）已决策为仅落库；待确认项 4（历史页）已实现为 `/history` + `/compare`。

---

## 1. 实现方案与框架选型

| 项 | 选型 | 理由 |
|---|---|---|
| ORM / DB 访问 | **SQLAlchemy 2.0（Core + ORM）** | DB 无关，Postgres/MySQL/SQLite 同一套 model；连接串走 `DATABASE_URL` 环境变量 |
| 同步 / 异步 | **同步（sync）** | 回测本身是 CPU 密集的同步计算，落库是秒级小批量写入，async 徒增复杂度 |
| 连接管理 | `create_engine` 模块级单例 + `sessionmaker` + 上下文 `with Session() as s` | 避免每请求建连；ThreadingHTTPServer 多线程安全（SQLite 需 `check_same_thread=False`） |
| 迁移 | **自研轻量 `init_db()` = `Base.metadata.create_all` + 可选 version 表**；**不引入 Alembic** | v1 表结构稳定，Alembic 过重且新增依赖；后续演进用少量 ALTER 脚本 |
| 双写策略 | **DB 为主 + 文件可选备份**（默认双写，DB 不可用时回退纯文件） | 保留人眼可复核的三件套，DB 作为唯一可追溯源 |
| 引擎改动面 | 仅改 `backtest.py`（落库一步）、`export_results.py`（文件写出可关）、`dashboard.py`（优先内存）、`web_app.py`（新接口） | 不动 `strategies/`、`indicators.py`、`data_feed.py`、`search.py` |

**连接串约定（环境变量）**
- `DATABASE_URL`（必选，缺省默认本地）：
  - 本地开发：`sqlite:///./strategy_lab.db`（**零额外依赖**，仅标准库）
  - Postgres：`postgresql+psycopg2://user:pass@host:5432/strategylab`
  - MySQL：`mysql+pymysql://user:pass@host:3306/strategylab`
- SQLite 多线程：`connect_args={"check_same_thread": False}`；生产库建议 `pool_pre_ping=True`。

---

## 2. 数据库表结构设计（SQLAlchemy 2.0 ORM 示意）

> 字段类型兼顾精度与 DB 无关：金额/价格用 `Numeric(18,4)`，百分比用 `Numeric(10,4)`，比率(sharpe)用 `Float`，
> 日期用 `Date`（评估/交易日期），`created_at` 用 `DateTime` 存 **UTC**。
> `run_id` 用 `String(36)` 存 UUID4（比原生 UUID 类型更跨库）。

### 2.1 模型（engine/storage/schema.py 骨架）
```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Date, DateTime, Numeric, Integer, Float, Text
import uuid, datetime

class Base(DeclarativeBase): pass

class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    run_id:        Mapped[str]  = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id:      Mapped[str|None] = mapped_column(String(36), index=True)   # 同一次提交(多标的)共享
    strategy_type: Mapped[str]  = mapped_column(String(64), index=True)        # 如 kdj_macd_dual_entry
    strategy_name: Mapped[str]  = mapped_column(String(128), index=True)
    symbol:        Mapped[str]  = mapped_column(String(32), index=True)        # 002001.SZ
    symbol_name:   Mapped[str]  = mapped_column(String(128))
    start:         Mapped[datetime.date] = mapped_column(Date, index=True)
    end:           Mapped[datetime.date] = mapped_column(Date, index=True)
    initial_cash:  Mapped[float] = mapped_column(Numeric(18,4))
    params_json:   Mapped[str]  = mapped_column(Text)        # 策略配置完整快照(可复跑)
    positions_json:Mapped[str|None] = mapped_column(Text)    # 合并持仓明细(派生视图)
    meta_json:     Mapped[str|None] = mapped_column(Text)    # market/generated_at/window_start_value/final_value
    created_at:    Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)
    equity = relationship("EquityPoint", cascade="all,delete-orphan")
    trades = relationship("Trade",        cascade="all,delete-orphan")
    summary = relationship("Summary",     cascade="all,delete-orphan", uselist=False)

class EquityPoint(Base):
    __tablename__ = "equity_points"
    id:    Mapped[int]  = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:Mapped[str]  = mapped_column(String(36), ForeignKey("backtest_runs.run_id"), index=True)
    date:  Mapped[datetime.date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Numeric(18,4))

class Trade(Base):
    __tablename__ = "trades"
    id:           Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:       Mapped[str]    = mapped_column(String(36), ForeignKey("backtest_runs.run_id"), index=True)
    entry_date:   Mapped[datetime.date] = mapped_column(Date)
    exit_date:    Mapped[datetime.date] = mapped_column(Date)
    side:         Mapped[str]    = mapped_column(String(8))     # long
    role:         Mapped[str|None]   = mapped_column(String(16)) # 底仓/做T
    position_id:  Mapped[str|None]   = mapped_column(String(36), index=True)
    size:         Mapped[int]    = mapped_column(Integer)
    entry_price:  Mapped[float]  = mapped_column(Numeric(18,4))
    exit_price:   Mapped[float]  = mapped_column(Numeric(18,4))
    pnl:          Mapped[float]  = mapped_column(Numeric(18,4))
    pnl_pct:      Mapped[float]  = mapped_column(Numeric(10,4))
    holding_bars: Mapped[int]    = mapped_column(Integer)
    symbol:       Mapped[str]    = mapped_column(String(32))
    symbol_name:  Mapped[str|None]  = mapped_column(String(128))
    display_symbol:Mapped[str|None] = mapped_column(String(128))
    label:        Mapped[str|None]  = mapped_column(String(64))

class Summary(Base):
    __tablename__ = "summary"
    run_id:           Mapped[str]  = mapped_column(String(36), ForeignKey("backtest_runs.run_id"), primary_key=True)
    total_return_pct: Mapped[float|None] = mapped_column(Numeric(10,4))
    annual_return_pct:Mapped[float|None] = mapped_column(Numeric(10,4))
    max_drawdown_pct: Mapped[float|None] = mapped_column(Numeric(10,4))
    sharpe:           Mapped[float|None] = mapped_column(Float)
    win_rate_pct:     Mapped[float|None] = mapped_column(Numeric(10,4))
    total_trades:     Mapped[int]    = mapped_column(Integer)
    meta_json:        Mapped[str|None] = mapped_column(Text)
```

### 2.2 索引建议
- `backtest_runs`: PK `run_id`；索引 `(symbol, created_at)`、`(strategy_name, created_at)`、`(created_at)` —— 支撑"历史列表按标的/策略/时间筛选"。
- `equity_points`: 索引 `(run_id, date)`（可选 `UNIQUE(run_id, date)`，同一 run 日线不重复）。
- `trades`: 索引 `(run_id)`、`(run_id, position_id)`。
- `summary`: PK `run_id`（即 FK）。

### 2.3 可选表（标注 Phase 2，本期不建）
- `strategies`：把 `strategies/*.toml` 也入库管理（含 `type/name/params_json/updated_at`）。**保留现状（文件）即可，本期不动**。
- `prices`：行情缓存入库（替代 `<prefix>_{daily,weekly}.csv`），解决"多线程并发覆盖"缺陷。**Phase 2 可选**。

---

## 3. 文件列表（新增 / 修改）

| 文件 | 动作 | 职责 |
|---|---|---|
| `engine/storage/__init__.py` | 新增 | 存储包导出 |
| `engine/storage/db.py` | 新增 | `get_engine()` 单例（读 `DATABASE_URL`，默认 SQLite）、`SessionLocal`、`init_db()`、`get_session()` 上下文 |
| `engine/storage/schema.py` | 新增 | `Base` + 4 个 ORM 模型 |
| `engine/storage/repository.py` | 新增 | `save_run()` / `get_run()` / `list_runs()` / `list_runs_by_ids()` / `delete_run()`（CRUD） |
| `engine/storage/serializers.py` | 新增 | DB 行 → dashboard `result` dict（与 `run_symbol` 返回同构），供 dashboard/web 复用 |
| `engine/storage/migrate.py` | 新增 | 轻量 `init_db()` + 可选 version 表（不引 Alembic） |
| `engine/backtest.py` | 修改 | `run_symbol` 增加落库步骤（默认双写；DB 不可用回退文件），返回追加 `run_id` |
| `engine/vendor/export_results.py` | 修改 | 新增 `write_files: bool = True` 参数（DB 为主时可关文件写出），防御校验保持不动 |
| `engine/dashboard.py` | 修改 | 对比线图改用内存 `equity_curve`（不读 CSV）；新增 `build_compare_from_runs(run_ids)` 经 repository 加载后复用渲染 |
| `web_app.py` | 修改 | 新增 `GET /api/runs`、`GET /api/runs/<id>`、`POST /compare`、`GET /history`；`POST /run` 落库后返回 |
| `run.py` | 修改 | 新增 `--no-db`/`--db`（默认按 `DATABASE_URL`）、`--list-runs` 列历史；CLI 也落库 |
| `requirements.txt`（或 `pyproject.toml`） | 修改 | 新增 `SQLAlchemy>=2.0`；driver 按需（`psycopg2-binary`/`pymysql`），本地默认零额外依赖 |
| `.env.example` | 新增 | 记录 `DATABASE_URL` 约定 |
| `tests/test_storage.py` | 新增 | SQLite 内存库验证建表/读写/查询（补"项目无测试"缺口） |
| `docs/存储改造设计.md`（本文件） | 新增 | 设计归档 |

---

## 4. 程序调用流程（Mermaid 时序图见 `docs/sequence-diagram.mermaid`）

### 4.1 改后 run_symbol 落库流程
```
run.py / web_app → run_symbol(cfg, sym, name, start, end, out_dir)
  1. normalize_symbol → ensure_data（取数/缓存，不变）
  2. strategy.run() → {equity_curve, trade_history, positions}   （不变）
  3. export_results(..., write_files=True) 写三件套 + _patch_trades_csv 补列   （不变/可选关文件）
  4. summary = 读 summary.json（不变）
  5. 【新增】if db available: run_id = repository.save_run(run_meta, equity_curve, trade_history, summary, positions)
  6. return {...原有字段..., "run_id": run_id}
```
> 落库失败不应中断回测：捕获 DB 异常，仅告警并回退纯文件（保证"回测永远能出图"）。

### 4.2 历史列表 → 选多 run → 对比仪表盘（Web）
```
Browser → GET /history        → web_app 渲染历史页(多选列表, 调 /api/runs)
Browser → GET /api/runs?symbol=&strategy=&limit=  → repository.list_runs()  → JSON(轻量摘要)
用户勾选多个 run_id → POST /compare {run_ids:[...]}
  web_app → repository.list_runs_by_ids(run_ids) → 每个 run 的 equity/trades/summary/positions
  web_app → dashboard.build_compare_from_runs(run_ids) 复用现有模块(多权益曲线+指标对比表+各 run 独立 Tab)
  → render_dashboard() → 返回自包含 HTML（沿用「← 新建回测」浮层）
```
> 跨初始资金对比细节：指标对比表显示各 run 原始 %；权益曲线建议**按各 run 窗口起点归一化到 100**（rebase）再叠加，
> 否则不同 `initial_cash` 的曲线不可比。归一化在 `build_compare_from_runs` 内做（见待确认项 6）。

---

## 5. 任务列表（有序、含依赖，按实现顺序；≤5 个，每组 ≥3 文件）

| ID | 任务 | 涉及文件 | 依赖 | 优先级 |
|---|---|---|---|---|
| **T01** | 项目基础设施与依赖声明（连接/会话单例 + 环境变量） | `requirements.txt`、`engine/storage/__init__.py`、`engine/storage/db.py`、`.env.example` | — | P0 |
| **T02** | 存储 Schema + Repository + 测试 | `engine/storage/schema.py`、`engine/storage/repository.py`、`tests/test_storage.py` | T01 | P0 |
| **T03** | 回测落库（双写） | `engine/backtest.py`、`engine/vendor/export_results.py`、`run.py` | T02 | P0 |
| **T04** | 仪表盘从 DB 读取与多 run 对比（Web 接口） | `engine/dashboard.py`、`web_app.py`、`engine/storage/serializers.py` | T02 | P1 |
| **T05** | 迁移脚本与文档收尾 | `engine/storage/migrate.py`、`docs/存储改造设计.md`、`README.md` | T02 | P2 |

> 依赖图：`T01 → T02 → {T03, T04}`、`T02 → T05`。T03 与 T04 互不依赖，可并行。

---

## 6. 依赖包列表

```
SQLAlchemy>=2.0          # 唯一硬依赖（DB 无关 ORM/Core）
# 以下为可选 driver，按用户提供的数据库类型安装（本地 SQLite 不需要）：
psycopg2-binary>=2.9     # 仅当使用 PostgreSQL
pymysql>=1.1             # 仅当使用 MySQL
# 明确不引入：Alembic（迁移用自研轻量 init_db 替代）
```
> 本地默认 `sqlite:///./strategy_lab.db` 仅用 Python 标准库，**零额外安装即可跑通**。
> 用户给出真实连接串后，仅补装对应 driver 即可切换，代码不变。

---

## 7. 共享知识（跨文件约定）

- **run_id**：`str(uuid.uuid4())`，`String(36)` 存储；**粒度 = 单个 (标的 × 策略配置 × 起始 × 结束)**。
  同一次多标的提交共享一个 `batch_id`（也是 UUID4），便于"回看我上一批提交"。
- **时间**：`created_at` 统一存 **UTC**（`datetime.now(timezone.utc)`）；评估/交易日期为**日历 Date**（无时区）。
- **金额精度**：价格/权益/盈亏 `Numeric(18,4)`；百分比 `Numeric(10,4)`；sharpe `Float`。
- **DATABASE_URL**：所有连接来源；`get_engine()` 模块级懒加载单例，`init_db()` 调 `create_all`。
- **Session 生命周期**：repository 内 `with SessionLocal() as s:` 短生命周期；不在请求间长期持有。
- **双写回退**：`run_symbol` 落库异常被吞掉并告警，保证回测产物（文件 + 内存）始终可用。
- **result dict 同构**：`serializers.db_row_to_result(run)` 返回与 `run_symbol` 完全同构的 dict，
  dashboard 无需感知数据来自文件还是 DB。
- **DB 无关类型**：一律用 `String`/`Numeric`/`Date`/`DateTime`，避免原生 `UUID`/`JSON` 列（不同库支持不一）。

---

## 8. 待确认事项（需用户 / 主理人决策）

1. **文件导出是否完全弃用？** 建议 **DB 为主 + 文件可选备份（默认双写）**；DB 不可用时回退纯文件。
2. **行情缓存（`prices` 表）是否也入库？** 建议 **Phase 2 可选**（顺带解决多线程并发覆盖缺陷）。本期不动 `data_feed.py`。
3. **策略 `.toml` 是否入库管理（`strategies` 表）？** 建议 **保持文件现状**，本期仅把配置快照存进 `params_json` 便于复跑。
4. **比对 UI 形态？** 建议 **在现有 `web_app` 增加「历史回测」页（`GET /history`）+ 多选 + 服务端渲染对比**，保持零额外前端依赖（沿用现有自包含 HTML 仪表盘）。
5. **`run.py` CLI 是否也落库？** 建议 **是**，默认按 `DATABASE_URL` 落库，提供 `--no-db` 关闭、`--list-runs` 查看历史。
6. **跨初始资金对比时权益曲线归一化方式？** 建议 **各 run 起点 rebased 到 100** 再叠加；指标表保留原始 %。或限制对比必须同 `initial_cash`。
7. **run 粒度**：确认 **每标的一行**（最灵活，支持任意跨策略/跨区间叠加），而非"每提交一批一行"。

---

## 9. 下一步需要用户提供的输入

1. **数据库类型与连接串（`DATABASE_URL`）**：
   - 若先用本地验证：默认 `sqlite:///./strategy_lab.db`（无需提供，零依赖）；
   - 若用 Postgres：`postgresql+psycopg2://user:pass@host:5432/strategylab`；
   - 若用 MySQL：`mysql+pymysql://user:pass@host:3306/strategylab`。
2. **上述 §8 待确认项的决策**（尤其 1、4、5、6）。
3. （可选）目标 DB 的字符集 / schema 名 / 是否需建库授权。
