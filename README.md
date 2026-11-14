# 配料线称重采集服务 (Weighing Line Collector)

FastAPI + SQLite 采集/查询服务：持续收集称重仪按日轮转的投料记录（无表头 UTF-8 CSV，
每行 `样品编号,称量时间,克重`），供事后追查。同一样品多次称量全部保留。

## 运行

```bash
docker compose up --build
```

三个容器：

| 服务 | 作用 |
|---|---|
| `app` | FastAPI API（采集 + 查询），SQLite 存于命名卷 `measurements-db` |
| `simulator` | 本地仪器模拟器：写当前日文件、半行写入、按"日"轮转（默认 45 秒加速）、轮转后向旧文件补写 |
| `collector-tick` | 可选的定时触发器，每 10 秒调一次 `POST /collect`（手动调用同样有效） |

宿主机 API 暴露在 `http://localhost:8000`（文档 `/docs`）。

## API

- `POST /collect` — 扫描采集目录，采入所有新的完整行。Body 可传
  `{"directory": "/data/incoming"}`，缺省用挂载的 `INCOMING_DIR`。
  返回每个文件的偏移推进、入库条数、拒收行数。
- `GET /measurements?sample_id=&since=&until=&limit=&offset=` — 查询测量记录
  （含来源文件与行号，便于追查）。
- `GET /files` — 已跟踪的文件身份（dev/ino）、已读偏移与行数。
- `GET /rejected` — 解析失败的完整行（审计用，不丢不重）。
- `GET /health`

## 正确性保证（设计要点）

1. **文件身份 = (st_dev, st_ino)**。轮转改名（rename）不改变 inode，
   因此改名后的旧文件被识别为同一文件，**不会重采**；`path` 仅作展示并随最新见到
   的名字更新。
2. **按字节偏移续采**。每个文件记录已消费的字节偏移与行数；旧文件轮转后继续补写
   的尾部，下次采集从偏移处接着读，**不会遗漏**。
3. **只采完整行**。每次只消费到最后一个 `\n` 为止；末尾半行（仪器尚未写完）留在
   文件中，待补齐后下次采入。`\n` 不可能出现在 UTF-8 多字节序列内部，故按 `\n`
   切分不会切断字符。
4. **测量记录与读取位置同一事务保存**。每轮采集是一个 `BEGIN IMMEDIATE` 事务：
   插入测量行与更新偏移要么一起提交、要么一起回滚。服务重启、进程崩溃不会产生
   重记/漏记；`measurements(file_id, generation, line_no)` 唯一约束兜底幂等。
5. **并发安全**。`BEGIN IMMEDIATE` + `busy_timeout` 使并发 `/collect` 在 SQLite
   写锁上串行；进程内另有互斥锁。多线程并发采集测试验证不重不漏。
6. **截断/inode 复用**。若文件小于已存偏移（被截断或 inode 被新文件复用），
   开启新 generation 从 0 重读，旧数据保留可查。

## 演示

```bash
# 终端 1
docker compose up --build

# 终端 2：观察模拟器的半行写入 / 轮转 / 补写日志
docker compose logs -f simulator

# 手动采集 + 查询
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{}'
curl 'localhost:8000/measurements?sample_id=SMP-0001'
curl 'localhost:8000/files'

# 一键演示脚本（含手工半行、手工轮转+补写；需把采集目录卷挂载到宿主机或
# 在容器内执行，INCOMING_DIR 指向采集目录）
./scripts/demo.sh
```

手工演示（在 `app` 容器内，目录为 `/data/incoming`）：

```bash
docker compose exec app sh -c 'printf "SMP-X1,2026-11-13T10:00:00,42.0" >> /data/incoming/weights_current.csv'
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{}'   # 半行不入库
docker compose exec app sh -c 'printf "\n" >> /data/incoming/weights_current.csv'
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{}'   # 补齐后恰好采入一次
```

## 本地测试（无需 Docker）

采集核心只依赖标准库，可直接运行：

```bash
python3 tests/test_collector.py   # 23 项：半行/轮转/补写/重启/并发/截断/坏行
```

## 目录结构

```
app/            FastAPI 服务（main.py）+ 采集核心（collector.py, 纯标准库）+ 存储层（db.py）
simulator/      仪器模拟器（半行写入、日轮转、轮转后补写）
scripts/demo.sh 演示脚本
tests/          采集核心测试
docker-compose.yml
```
