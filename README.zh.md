# s3rds

**真·serverless 的 RDS：Lambda URL 就是数据库 endpoint，S3 上的 SQLite 就是存储，闲时 ≈ $0。**

[English](README.md) · *Redefine Serverless* 系列 —— 用 AWS 原语重做那些"闲着也要钱"的托管服务。

![s3rds 架构](docs/architecture.svg)

| | **s3rds** | RDS `db.t4g.micro` 单 AZ | RDS `db.t4g.micro` Multi-AZ | Aurora Serverless v2 |
|---|---|---|---|---|
| 闲时月费（us-east-2） | **≈ $0**（只有 S3 存储） | ~$14 | ~$28 | ~$45（0.5 ACU 底价） |
| 可用区 | 区域级，天然多 AZ | 一个 | 两个 | 多 |
| 实例故障 | 没有实例 | 停机数分钟 | 故障转移 ~60 秒 | 故障转移 |
| 维护窗口 | 无 | 有，会重启 | 有 | 有 |
| SLA | Lambda 99.95% · S3 99.9% | 单 AZ 无 SLA | 99.95% | 99.99% |
| 持久性 | S3，11 个 9 | EBS | EBS 复制 | 6 副本 |
| SQL 兼容性 | SQLite 方言，或经 `s3rds.pymysql` 的 MySQL 方言——见自动生成的[兼容性报告](tests/compat/REPORT.md) | MySQL | MySQL | MySQL / PostgreSQL |

> 设计原则：这是一个关系型数据库，数据正确性第一——原子性、一致性不妥协，
> 写入可以失败，但绝不重复、绝不写错。优势是省钱（闲时 ≈ $0），代价是性能一般。
> 取舍顺序：正确性 > 成本 > 性能。

## 为什么

AWS 的"serverless"大多指**实例会自动伸缩**，很少指**根本没有实例**。
最便宜的 provisioned RDS 也要 ~$14/月且没有 SLA；Aurora Serverless v2 在底价也要 ~$45/月。

s3rds 没有实例，两次请求之间什么都不在运行。Lambda 在永久免费额度内
（每月 100 万次请求 + 40 万 GB·秒），超出后 $0.20/百万次。

## 怎么工作

- **一个库 = S3 上一个 `.sqlite` 文件。** db lambda 把它拉到 `/tmp`、执行 SQL，**只有数据真的变了**才整文件回传
  （SQLite authorizer + 文件指纹双重判定）。
- **读很便宜**：对着 `/tmp` 热缓存比一下 ETag，服务端 ~15 ms，不下载。
- **写很安全**：每条请求一个显式事务（DDL 也在内）；上传是 `If-Match` 条件写（CAS）。冲突就重拉重放，
  重试耗尽就干净失败并作废本地副本。永远没有幽灵数据。
- **天然单写者**（`reserved_concurrency = 1`），另有一个**只读 endpoint**不限并发（同一份代码，`READONLY=1`）。

对应 RDS 的概念：

| RDS | s3rds |
|---|---|
| 实例 endpoint | db lambda 的 Function URL |
| 实例里的一个 database | 桶里的一个 `.sqlite` 对象 |
| 连接串里的 `database=` | URL 路径 `/api/db/{name}` |
| 用户名 / 密码 | HTTP Basic（`db_user` / `db_password`） |
| 数据目录 | `s3://<bucket>/db/` |
| `CREATE DATABASE` | `POST /api/databases` |

## 快速开始

### 1. 部署服务端（二选一）

**Terraform**

```bash
cp terraform/example.json terraform/my.json      # 改 aws_profile、env、db_password、auth_token
cd terraform
terraform init
terraform apply -var-file=my.json
terraform output                                  # db_url、db_ro_url、demo_url
```

**CloudFormation**（只需要 AWS CLI）

```bash
cp cloudformation/example-params.json cloudformation/params.json   # 改密码
cp client/sqlite.py src/demo/                                   # demo 要带上驱动
aws cloudformation package --template-file cloudformation/template.yaml \
    --s3-bucket <你自己任意一个桶> --output-template-file /tmp/s3rds.yaml
aws cloudformation deploy --template-file /tmp/s3rds.yaml --stack-name s3rds \
    --capabilities CAPABILITY_IAM --parameter-overrides file://cloudformation/params.json
```

两种方式建出来的东西一样：一个 S3 桶（`<env>-srds-db-2026`）、三个 Lambda（读写、只读、demo）
和它们的公网 Function URL、一个最小权限 IAM role。S3 桶名全球唯一，被占了就改后缀。

### 2. 打开控制台

浏览器打开 `db_url`，用 `db_user` / `db_password` 登录。建库、建表（默认 STRICT）、加列、改行、跑 SQL。
每次操作都显示服务端耗时，以及这次是命中 `/tmp` 缓存还是去了 S3。

### 3. 程序连接

```bash
pip install s3rds              # pys3sqlite:SQLite 方言,零依赖
pip install "s3rds[mysql]"     # + pys3mysql:写 MySQL,客户端翻译(sqlglot)
```

想写哪种方言就 import 哪个，API 都是 pymysql 形状；将来上真 RDS 就是最后一次 `import pymysql`：

```python
from s3rds import sqlite                            # SQLite 方言
# from s3rds import pymysql                         # MySQL 方言(见下面「MySQL 方言」)

conn = sqlite.connect(host="xxxx.lambda-url.us-east-2.on.aws",
                         user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(sqlite.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())

conn.transaction([                                   # 全成或全败,一次 S3 写入
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

零依赖（只用标准库）。线协议就是 HTTPS + JSON，任何语言都能接：

```bash
curl -s -u "admin:密码" -H "content-type: application/json" \
     -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     "https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query"
```

## 兼容性

兼容性矩阵是自动生成的，不是手写的：**[tests/compat/REPORT.md](tests/compat/REPORT.md)**。
每一行是一项能力（DDL、DML、查询、函数、约束、元数据、类型），用同一条 SQL 在参照库——`s3rds.pymysql` 对
Docker 里的 MySQL 8.0，`s3rds.sqlite` 对标准库 `sqlite3`——和 s3rds 上各跑一遍，比对结果、Python 类型、
rowcount、lastrowid 和错误码。

| 状态 | 含义 |
|---|---|
| ✅ verified | 与参照库一致，可放心依赖 |
| ❌ gap | 已知差异，备注写明哪个任务修 |
| ⛔ unsupported by design | 明确拒绝（`NotSupportedError`：行锁、触发器、存储过程），绝不静默走样 |
| 🔴 regression | 以前一致现在不一致，阻止发版 |

重新生成报告（MySQL 参照库跑在 Docker 里）：

```bash
docker compose -f tests/compat/docker-compose.yml up -d mysql
MYSQL_DSN=mysql://root:test@127.0.0.1:3306/compat python3 -m pytest tests -q
docker compose -f tests/compat/docker-compose.yml down
```

## 保证

| | 靠什么 |
|---|---|
| **原子性** | 每条请求显式 `BEGIN/COMMIT`（DDL 也在内）+ S3 PUT 本身原子；`/tx` 多条语句一个事务 |
| **一致性** | SQLite 约束；新表默认 `STRICT`；每个写连接 `PRAGMA foreign_keys=ON` |
| **隔离性** | 单写者（reserved concurrency 1 + `If-Match` CAS）；读到的是整文件快照 |
| **持久性** | S3 PUT 成功后客户端才收到 OK（11 个 9） |
| **绝不重复写** | 驱动只在"请求确定没送达"时重发；超时一律报"结果未知" |
| **可用性** | Lambda 和 S3 是区域级、天然多 AZ 的服务：没有实例会挂、没有 AZ 可丢、没有维护窗口。高于单 AZ 的 RDS 实例（单 AZ 没有 SLA）；RDS 要开 Multi-AZ 花两倍钱才追平 |

**事务，说具体点。** 每条语句在 `execute()` 返回时就已提交并落到 S3。`commit()` 是空操作。
`rollback()` **撤销不了已经执行的语句**——上次 `commit()` 之后有过写入它会发警告，没有就保持安静
（连接池和 ORM 归还连接时会调它）。多条语句要同成同败，用 `conn.transaction([...])`（一次提交、一次 S3 写入）
或 `cursor.executemany(...)`。

**大结果集。** 服务端按 4 MB 分页，驱动在 `execute()` 里取完所有页再返回，结果永远完整，绝不静默截断。
翻页期间库被改了，服务端拒绝混合两个版本，驱动自动重跑查询。

这些承诺由测试钉死（内存假 S3 实现了 ETag / `If-Match` / `If-None-Match` 语义）：

```bash
pip install pytest boto3
pytest tests/ -v
```

## MySQL 方言（`s3rds.pymysql`）

写 MySQL，跑在同一个 endpoint 上。语句在客户端翻译（sqlglot，按语句缓存）：

- `%s` / `%(name)s` 占位符、反引号、`LIMIT offset, count`
- `AUTO_INCREMENT` → `INTEGER PRIMARY KEY AUTOINCREMENT`；`ENUM(...)` → `TEXT CHECK (...)`；`UNSIGNED` → 有符号；
  文本列加 `COLLATE NOCASE`（模拟 MySQL 默认不区分大小写；`_bin` 排序规则保持区分）
- 行内 `KEY` / `INDEX` / `UNIQUE KEY name (a, b)` 变成同一事务里的 `CREATE INDEX`
  （存为 `table__name`，SHOW / DROP 时按 MySQL 的名字）
- `INSERT IGNORE`、`ON DUPLICATE KEY UPDATE`（`VALUES(col)` → `excluded.col`）、`REPLACE INTO`、`INSERT … SELECT`、
  `DEFAULT` 关键字、`UPDATE … JOIN`、`DELETE t FROM t JOIN …`、`TRUNCATE`、`RENAME TABLE`
- `ALTER TABLE CHANGE a b` 改名；只改类型的 `MODIFY` / `CHANGE` 接受但不做事（SQLite 动态类型）
- 函数：`DATE_ADD/SUB`、`YEAR … SECOND`、`DAYOFWEEK`、`DATEDIFF`、`TIMESTAMPDIFF`、`LPAD/RPAD`、`REPEAT`、
  `SUBSTRING_INDEX`（count ±1）、`->` / `->>` / `JSON_UNQUOTE`、`UUID()`、`RAND()`、`LAST_INSERT_ID()`、`DATABASE()`
- `SHOW TABLES / DATABASES / COLUMNS / CREATE TABLE / INDEX`、`DESCRIBE`、`information_schema.tables / columns`；
  `USE db`、`SET NAMES / SESSION / sql_mode / time_zone` 接受为空操作
- 报错是 pymysql 风格的异常类 + MySQL 错误码：1062 重复键、1146 表不存在、1054 列不存在、1064 语法……
- `ENGINE=`、`CHARSET=`、`COMMENT`、`ON UPDATE CURRENT_TIMESTAMP` 会被丢弃

不支持的——抛 `NotSupportedError`，绝不静默出错：存储过程/函数、触发器、`FULLTEXT`、
`SELECT … FOR UPDATE`、`LOCK TABLES`、`GRANT`/用户管理。

结果类型和 pymysql 一致：`DATETIME` → `datetime`、`DATE` → `date`、`TIME` → `timedelta`、`DECIMAL` /
`SUM` / `AVG` / 整数除法 → `Decimal`、`BLOB` → `bytes`（双向）、`NOW()` → `datetime`。
模拟严格模式：`'abc'` 插进 `INT` 列报 1366。`DESCRIBE` 和 `SHOW CREATE TABLE` 显示你声明的 MySQL 类型
（存在库里一张小表 `_s3rds_columns`）。

**兼容性报告 —— [tests/compat/REPORT.md](tests/compat/REPORT.md)。** 上面每一项能力对应一个测试：同一条 SQL
在 Docker 里的真 MySQL 8.0 和 s3rds 上各跑一遍，比对结果、Python 类型、rowcount、lastrowid 和错误码。
verified 的可以放心用；gap 的标了哪个任务来修。每次运行自动重新生成（命令见上面[兼容性](#兼容性)一节）。

## 适用规模

| 单库大小 | 结论 |
|---|---|
| **< 10 MB** | 甜点区：热读 20–50 ms，冷读/写 ~300 ms，无感知 |
| **10–20 MB** | 设计极限：写几百 ms，手感仍流畅 |
| **20–50 MB** | 能用，但每次写整文件回传（~1 s），写频繁会烦 |
| **> 50 MB** | 换真 RDS，见 [MIGRATION.md](MIGRATION.md) |

读不随大小退化（ETag 缓存），只有冷读和写要搬整个文件。写吞吐每秒几次。
适合个人应用、内部工具、博客、MVP；不适合高并发 OLTP。

`scripts/bench.py` 对你的部署测端到端延迟和吞吐，跑完自己清理。

## 目录

```
src/db/           数据库本体:lambda_function.py(HTTP API)、s3_sqlite.py(引擎)、index.html(控制台)
src/demo/         业务 Lambda 示例,把 endpoint 当 RDS 用
client/           pip 包 s3rds:s3rds/sqlite.py、s3rds/pymysql.py、s3rds/psycopg2.py、pyproject.toml
terraform/        部署方式 A
cloudformation/   部署方式 B
tests/            正确性测试
scripts/          bench.py
MIGRATION.md      长大了怎么迁 RDS MySQL
```

## 路线图

- `s3rds.pymysql`：`DATETIME`/`DECIMAL` 结果类型转换、更多 MySQL 函数、公开的兼容性报告。
- `s3rds.psycopg2`：PostgreSQL 方言（占位模块已随包发布，`connect()` 在实现前会直接报错）。
- CloudFormation 一键 *Launch Stack* 按钮。

## Redefine Serverless 系列

那些"闲着也计费"的 AWS serverless 服务，用闲时 ≈ $0 的原语重做一遍。
统一规则：项目名 = pip 包名 = import 名；子模块 = 它模仿的标准库名，切回真服务只改一行 import。

| 替代 | 闲时底价/月 | 原语 | 项目 | 用法 | 状态 |
|---|---|---|---|---|---|
| RDS / Aurora Serverless | ~$14–45 | S3 + Lambda | **s3rds** | `from s3rds import pymysql` · `sqlite` · `psycopg2` | 本仓库 |
| Redis / ElastiCache Serverless | ~$90 | DynamoDB | **pyredis** | `from pyredis import redis` | 下一个 |
| Airflow / MWAA | ~$360 | Lambda + EventBridge + Step Functions | **lambdaflow** | `from lambdaflow import airflow` | 计划 |
| Redshift Serverless | 一跑 ~$3/h | S3 + DuckDB | **s3shift** | `from s3shift import redshift_connector` | 计划 |
| OpenSearch Serverless | ~$350 | S3 + SQLite FTS5（复用 s3rds） | **s3search** | `from s3search import opensearchpy` | 计划 |
| Kafka / MSK Serverless | ~$550 | SQS | **sqskafka** | `from sqskafka import kafka` | 计划 |
| DocumentDB（MongoDB） | ~$200 | SQLite JSON1（复用 s3rds） | **s3mongo** | `from s3mongo import pymongo` | 计划 |
| Amazon MQ（RabbitMQ） | ~$20–50 | SQS | **sqsmq** | `from sqsmq import pika` | 计划 |

顺带覆盖：MemoryDB（→ pyredis）、Kinesis Data Streams（→ sqskafka）、Timestream（→ s3shift）。

共同原则：闲时 ≈ $0、无容器无实例、drop-in 标准接口、同一套测试跑真服务和重做版对照、MIT。

## 协议

MIT
