# 配料线称重采集与交班凭证服务 (Weighing Line Collector & Handover Vouchers)

FastAPI + SQLite 服务：持续收集称重仪按日轮转的投料记录（无表头 UTF-8 CSV，
每行 `样品编号,称量时间,克重`），供事后追查；并为交班员生成**不可变的交班凭证
CSV**，交给追溯人员。同一样品多次称量全部保留。

## 运行

```bash
docker compose up --build
```

宿主机 API 暴露在 `http://localhost:8000`（交互式文档 `/docs`，可直接在页面上
试所有接口）。

| 服务 | 作用 |
|---|---|
| `app` | FastAPI API（采集 + 查询 + 凭证），SQLite 存于命名卷 `measurements-db`，凭证 CSV 存于 `vouchers` |
| `simulator` | 本地仪器模拟器：写当前日文件、半行写入、按"日"轮转（默认 45 秒加速）、轮转后向旧文件补写 |
| `collector-tick` | 可选的定时触发器，每 10 秒调一次 `POST /collect`（手动调用同样有效） |

## API

采集与查询：

- `POST /collect` — 扫描采集目录，采入所有新的完整行。Body 可传
  `{"directory": "/data/incoming"}`，缺省用挂载的 `INCOMING_DIR`。
- `GET /measurements?sample_id=&since=&until=&limit=&offset=` — 查询测量记录
  （含来源文件与行号，便于追查）。
- `GET /files` — 已跟踪的文件身份（dev/ino）、已读偏移与行数。
- `GET /rejected` — 解析失败的完整行（审计用，不丢不重）。

交班凭证：

- `POST /vouchers` — Body `{"request_id","sample_id","since","until"}`。
  **首次受理即冻结**当时已入库的匹配记录及来源（同一事务写入
  `voucher_records`），后台线程生成 CSV。返回 `202` 与凭证状态
  （`PENDING/GENERATING/COMPLETED/FAILED`）。
- `GET /vouchers` / `GET /vouchers/{request_id}` — 状态、失败原因、记录数，
  以及冻结的记录集（测量编号、原始三列、来源文件、代次、行号）。
- `POST /vouchers/{request_id}/retry` — 失败后用**同一冻结记录集**重新生成；
  已完成的凭证不可重试（409）。
- `GET /vouchers/{request_id}/download` — 下载凭证 CSV；**仅 COMPLETED 可下载**，
  其余状态返回 404。
- `GET /health`

凭证 CSV 为带 BOM 的 UTF-8（Excel 直接打开中文不乱码），列：

```
测量编号,样品编号,称量时间,克重,来源文件,代次,行号
```

## 凭证正确性保证（设计要点）

1. **受理即冻结，交付后不随补采改变。** 首次受理在一个 `BEGIN IMMEDIATE`
   事务里把匹配测量行（测量 id、原始三列逐字保留）连同来源文件路径、代次、
   行号复制进 `voucher_records`。之后采集到的晚到记录只进 `measurements`，
   永远不会加入既有凭证；重试或进程重启也沿用原记录集与原来源描述。
2. **请求标识幂等 + 参数冲突。** `request_id` 唯一：同标识同参数返回原凭证
   （`created=false`，同一文件）；同标识不同参数返回 `409` 并附带原凭证信息。
3. **并发只生成一份。** 并发首次提交在 SQLite 写锁上串行，唯一约束兜底；
   后台任务用 `PENDING -> GENERATING` 原子认领，一个 API 进程内同一凭证只会
   生成一个 CSV。
4. **生成不阻塞采集与查询。** 冻结是短事务，CSV 在后台线程写临时文件；
   `/collect`、`/measurements` 全程可用。
5. **原子写 + 崩溃恢复。** 先写 `voucher_<id>.csv.tmp` 并 `fsync`，再
   `os.replace` 原子改名、fsync 目录，最后才把状态置为 COMPLETED。启动时
   `recover()`：
   - 状态停在 GENERATING 且成品文件存在且内容与冻结集一致 → **直接采纳成品**
     （覆盖"文件完成与状态保存之间退出"的窗口），不重建；
   - 成品不存在或不完整 → 清除半成品、重新排队生成；
   - 所有残留 `.tmp` 一律清理；已完成凭证一律不动。
6. **写失败可恢复。** 任何生成异常都记为 FAILED 并保存 `failure_reason`、
   删除半成品；`retry` 重新排队，成功后才可下载。
7. **来源可追溯且被冻结。** 来源文件按 (dev, inode) 识别、支持轮转改名；
   凭证里存的是受理时刻的路径快照，源文件之后改名/截断都不影响凭证。

## 采集侧正确性保证（设计要点）

1. **文件身份 = (st_dev, st_ino)**。轮转改名（rename）不改变 inode，
   因此改名后的旧文件被识别为同一文件，**不会重采**；`path` 仅作展示并随最新
   见到的名字更新。
2. **按字节偏移续采**。每个文件记录已消费的字节偏移与行数；旧文件轮转后继续
   补写的尾部，下次采集从偏移处接着读，**不会遗漏**。
3. **只采完整行**。每次只消费到最后一个 `\n` 为止；末尾半行（仪器尚未写完）
   留在文件中，待补齐后下次采入。`\n` 不可能出现在 UTF-8 多字节序列内部。
4. **测量记录与读取位置同一事务保存**。每轮采集是一个 `BEGIN IMMEDIATE` 事务，
   服务重启、进程崩溃不会产生重记/漏记；
   `measurements(file_id, generation, line_no)` 唯一约束兜底幂等。
5. **并发安全**。`BEGIN IMMEDIATE` + `busy_timeout` 使并发 `/collect` 在 SQLite
   写锁上串行；进程内另有互斥锁。
6. **截断/inode 复用**。若文件小于已存偏移，开启新 generation 从 0 重读，
   旧数据保留可查。

## 演示

### 一键脚本

```bash
docker compose up --build          # 终端 1
./scripts/demo_vouchers.sh         # 终端 2：生成/补采不变/失败重试/崩溃恢复
./scripts/demo.sh                  # 采集侧原有演示（半行、轮转、补写）
```

### 在 /docs 页面手工演示（推荐）

`docker compose up --build` 后打开 `http://localhost:8000/docs`：

1. 先 `POST /collect` 几次，再用 `GET /measurements` 取一个真实 `sample_id`
   和时间窗口。
2. `POST /vouchers` 提交（如 `request_id=v1`），轮询 `GET /vouchers/v1` 到
   `COMPLETED`，用 `GET /vouchers/v1/download` 下载；再次提交同标识同参数
   看到 `created=false`，改参数看到 409。
3. 再 `POST /collect` 触发补采，重新请求 `v1`：记录数与 CSV 哈希不变。
4. **失败 + 重试**：compose 预置 `VOUCHER_FAIL_REQUEST_IDS=demo-fail`，
   提交 `request_id=demo-fail` 会得到 FAILED（含原因、不可下载），调
   `POST /vouchers/demo-fail/retry` 后成功下载。
5. **崩溃恢复**：compose 预置 `VOUCHER_CRASH_AFTER_RENAME=demo-crash`，
   提交 `demo-crash` 后进程在 CSV 写完改名后、状态保存前硬退出，Docker
   自动重启；重启后 `GET /vouchers/demo-crash` 已是 COMPLETED 且可下载
   （成品被采纳，未重建）。容器日志可见 `[voucher-recover] adopted ...`。

手工半行/轮转演示（在 `app` 容器内）：

```bash
docker compose exec app sh -c 'printf "SMP-X1,2026-11-13T10:00:00,42.0" >> /data/incoming/weights_current.csv'
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{}'   # 半行不入库
docker compose exec app sh -c 'printf "\n" >> /data/incoming/weights_current.csv'
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{}'   # 补齐后恰好采入一次
```

## 本地测试（无需 Docker，纯标准库）

```bash
python3 tests/test_collector.py   # 23 项：半行/轮转/补写/重启/并发/截断/坏行
python3 tests/test_voucher.py     # 57 项：冻结/幂等/冲突/并发/补采不变/
                                  #       失败重试/崩溃恢复/半成品清理/CSV 内容
```

## 目录结构

```
app/
  main.py        FastAPI 路由（采集、查询、凭证）
  collector.py   采集核心（纯标准库）
  voucher.py     凭证核心：冻结快照、后台生成、原子写、崩溃恢复（纯标准库，含测试用 CLI）
  db.py          SQLite schema 与连接
simulator/       仪器模拟器（半行写入、日轮转、轮转后补写）
scripts/         demo.sh（采集）、demo_vouchers.sh（凭证全流程）
tests/           采集与凭证的端到端测试
docker-compose.yml
```
