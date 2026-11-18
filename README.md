# 单队列竞争风险分析 API（30 天再入院累计发生概率）

面向公共卫生队列数据员：评估出院患者 **30 天内再住院的累计发生概率（CIF）**，
**死亡作为竞争结局**（不当作普通删失）。技术栈：FastAPI + SQLite + 后台串行重算，
提供 Docker Compose 一键启动与 Swagger 操作入口。

内置**事件证据对账**：数据员用院内事件记录核实每人的终局（避免漏记死亡、
避免把已再入院者记作随访截止）；**仅证据核对通过且分析完成的现行风险结果可供数**。

## 1. 统计方法

逐人记录结局发生日（1~30）与结局（再入院 / 死亡 / 随访截止），按 **Aalen-Johansen
乘积极限估计**计算竞争风险 CIF：

- 日初在险人数 `Y_t`：第 t 天开始时尚未发生任何事件、未删失的人数；
  **同日再入院与死亡共用同一个日初在险人数**。
- 原因别危险：`h1_t = d1_t / Y_t`（再入院）、`h2_t = d2_t / Y_t`（死亡）。
- 当日生存因子：`S_t = S_{t-1} × (1 − (d1_t + d2_t) / Y_t)`。
- CIF 增量：`ΔCIF_k(t) = S_{t-1} × h_k(t)`，逐日累加。
- **随访截止（CENSORED）者在当日事件计数之后才退出风险集**，不进入当日危险分母；
  **死亡进入事件计数并压低生存因子**，绝不按普通删失处理。
- 恒等式：`CIF_再入院 + CIF_死亡 + S(30) = 1`（测试中逐日校验）。

## 2. CSV 格式

UTF-8 编码、必含三列（表头大小写不敏感、列序不限）：

| 列名 | 说明 |
| --- | --- |
| `subject_id` | 每人唯一编号，非空、不可重复 |
| `day` | 结局发生日，1~30 整数 |
| `outcome` | `READMISSION` / `DEATH` / `CENSORED`，兼容中文 `再入院` / `死亡` / `随访截止` 及常见别名 |

示例见 [`samples/`](samples/)。

## 2.1 事件证据 CSV 与对账规则

数据员用院内事件记录核实队列终局。证据 CSV 针对**现行队列**上传，UTF-8 编码，
必含 `subject_id`、`day` 两列及事件列（`event` / `event_type` / `outcome` 任一）：

| 列名 | 说明 |
| --- | --- |
| `subject_id` | 患者号，**同一患者可有多条**（多次事件） |
| `day` | 事件发生日，1~30 整数 |
| `event` | `READMISSION` / `DEATH`，兼容中文 `再入院` / `死亡` 及常见别名；随访截止不是事件，不得出现 |

仅表头无数据行是合法空证据（全队列无事件时使用）。上传即对账（同一事务内完成），
按每患者**最早**证据事件与队列终局比对，逐人报告四类问题：

| issue_type | 含义 |
| --- | --- |
| `AMBIGUOUS_FIRST_DAY` | 首发日两类事件并存（最早日同时有再入院与死亡），须人工核实 |
| `EVENT_NOT_RECORDED` | 证据最早事件不晚于队列结局日，但队列未记录（漏记死亡、已再入院者被记作随访截止、队列事件日更晚或同日类型不符） |
| `MISSING_EVIDENCE` | 队列已记事件（再入院/死亡），但证据中找不到同日同类记录 |
| `UNKNOWN_SUBJECT` | 证据患者号不在现行队列中 |

事件晚于队列随访截止日的，不属本队列观察责任范围，不报问题。
首发日歧义的患者只报歧义，待核实后再核对。全部通过（零问题）为 `PASSED`，
否则 `FAILED` 并逐人列出问题。

## 2.2 供数门禁与资格撤销

- **仅证据核对通过且分析完成的现行风险结果可供数**：`/api/analyses/current`
  （及现行版本的 `/api/analyses/{revision}`）在分析未完成时返回 409
  `ANALYSIS_*`；分析完成但无有效证据时返回 409 `EVIDENCE_MISSING`；
  对账未通过时返回 409 `EVIDENCE_RECONCILIATION_FAILED`（附问题数）。
  历史（SUPERSEDED）版本的显式查询不受门禁阻断，仅供审计，标注 `is_current=false`。
- **替换证据**（同修订不同内容）：旧批次立即置 `SUPERSEDED`，旧核对资格同时撤销，
  新批次重新对账；**同版同内容重送幂等**，沿用既有批次与结论，不产生任何改动。
- **受理新版队列**：旧修订的有效证据批次在同一事务内立即撤销；新修订必须重新
  上传证据并对账，**绝不借用旧版的证据或结论**。
- 对账在证据受理事务内**同步**完成（无异步对账）；证据可显式指定目标修订号，
  与现行不一致即 409 拒收，防止"上传期间队列被替换"挂错版本。
- 全部对账状态持久化于 SQLite：服务重启后从数据库恢复，未核实版本在重启前后
  都不可供数。

## 3. 修订与一致性规则

- 修订版本号为**递增整数**，每次上传为**整体更换**；下一可受理版本 = 当前最大版号 + 1。
- **同版同内容**（按原始字节 SHA-256 判定）重送：幂等，不产生任何改动。
- **同版异内容**或跳号：HTTP 409 拒收。
- 新修订在单个 `BEGIN IMMEDIATE` 事务内受理：旧 CURRENT 立即置 `SUPERSEDED`，
  其未完成分析立即置 `SUPERSEDED`；新修订写入 `PENDING`。事务提交后任何查询
  都不可能再把旧结论当作现行结论返回。
- 重算由**单后台守护线程串行**执行；落库前再次校验该修订仍为 CURRENT，
  计算期间被取代则结果丢弃、状态转 `SUPERSEDED`，**绝不发布旧修订结论**。
- 重算失败落 `FAILED`、服务重启后遗留的 `PENDING` 自动恢复重算；两种状态下
  `/api/analyses/current` 均返回 409 状态提示而非旧结论，可用
  `POST /api/analyses/recompute` 手动重试。
- 显式查询某历史版本 `/api/analyses/{revision}`：若该版本已完成则返回结果并带
  `is_current=false`（审计留痕）；若其在计算中被取代（从未发布）则返回 409。

## 4. API 一览（Swagger：`/docs`，OpenAPI：`/openapi.json`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查（含供数状态） |
| POST | `/api/revisions/upload` | 表单字段 `revision` + 文件 `file`，上传完整队列 CSV |
| GET | `/api/revisions` | 修订清单与分析状态 |
| GET | `/api/analyses/current` | 现行修订逐日结论（须分析完成且证据对账通过） |
| GET | `/api/analyses/{revision}` | 指定修订结论（历史版本标注 `is_current=false`） |
| POST | `/api/analyses/recompute` | 手动重算现行修订（可带 `?revision=`） |
| POST | `/api/evidence/upload` | 文件 `file`（可带 `revision`），上传事件证据 CSV 并立即对账 |
| GET | `/api/evidence/current` | 现行修订证据批次、逐人对账问题与供数状态 |
| GET | `/api/evidence/{revision}` | 指定修订证据批次与对账结论（含已撤销批次，审计留痕） |
| GET | `/api/serving` | 供数状态总览（分析状态 + 证据状态 + 是否可供数） |

逐日响应字段：`day, at_risk, readmission_count, death_count, censored_count,
readmission_cif, death_cif, survival`（覆盖 1~30 全天，无事件日 CIF 不变）。
分析响应附 `evidence_status`（`NONE` / `PASSED` / `FAILED`）。

## 5. 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build -d
# Swagger: http://localhost:8000/docs
```

SQLite 数据库与暂存目录保存在命名卷 `cohort-data`（容器内 `/data`），
重启不丢数据；自带 healthcheck。

### 本地直接运行

```bash
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可用环境变量（见 `.env.example`）：`COHORT_DB_PATH`、`COHORT_UPLOAD_DIR`、
`COHORT_HORIZON_DAYS`（默认 30）、`COHORT_MAX_UPLOAD_BYTES`（默认 50 MB）。

### curl 示例

```bash
curl -X POST http://localhost:8000/api/revisions/upload \
  -F revision=1 -F "file=@samples/cohort_rev1.csv"
# 分析完成但无证据时：409 EVIDENCE_MISSING
curl -X POST http://localhost:8000/api/evidence/upload \
  -F revision=1 -F "file=@samples/events_rev1.csv"
curl http://localhost:8000/api/analyses/current
# 问题证据演示（四类逐人问题）：samples/events_rev1_with_problems.csv
```

## 6. 自测

```bash
python3 -m pip install pytest httpx
python3 -m pytest tests/ -v
```

34 个用例覆盖：Aalen-Johansen 手算对照、同日事件共用风险集、删失当日事件后退出、
死亡不被当作删失、上传幂等、同版异内容拒收、版号递增、CSV 校验、
**修订交错（计算中被取代绝不发布）**、**重启恢复与 FAILED 重试不返回旧结论**、
**首发日歧义**、事件未记录/缺证据/未知患者逐人对账、**供数门禁**、
**证据替换与新队列受理撤销旧核对资格**、证据幂等、**证据与分析交错**、
**重启后对账状态恢复**。

## 7. 目录结构

```
app/            FastAPI 应用
  main.py         路由与统一错误处理（lifespan 启动恢复；供数门禁）
  analysis.py     Aalen-Johansen 竞争风险 CIF
  reconcile.py    事件证据对账（纯函数，按最早事件核对）
  csv_parser.py   队列/证据 CSV 解析、校验、SHA-256
  database.py     SQLite 模式、IMMEDIATE 事务、版本/证据受理与落库
  services.py     后台串行重算线程与启动恢复
  schemas.py      Pydantic 模型
  config.py       环境变量配置
samples/        示例 CSV（队列含中文结局版本；证据含通过版与四类问题演示版）
tests/          pytest 测试
Dockerfile / docker-compose.yml / .env.example
```

## 8. 说明与限制

- 单进程部署：串行化依赖进程内写锁 + SQLite 事务；多副本水平扩展需换用
  外部队列/选主（当前需求为单队列 API，未实现）。
- 时间尺度固定为 1~30 天的离散日尺度；每人一条终局记录。
- 原始 CSV 解析后即删除暂存文件，仅持久化规范化记录、证据事件与分析/对账结果。
- 对账只核到"队列终局与证据最早事件一致"这一层；证据本身的真实性
  （是否伪造、是否完整覆盖全院事件）需由数据员所在流程保证。
