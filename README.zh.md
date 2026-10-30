# s3rds

![s3rds 架构](docs/architecture.svg)

### 切换到 s3rds —— 唯一要改的地方

| 你的代码用的是 | 原来 | 换成 s3rds —— **唯一的改动** | 真正的区别 |
|---|---|---|---|
| **sqlite3** | `import sqlite3`<br>`sqlite3.connect("app.db")` | `from s3rds import sqlite`<br>`sqlite.connect(host=URL, user=…, password=…, database="app")` | 文件在 S3 上、走 HTTPS；`create_function` 被拒绝 |
| **pymysql** | `import pymysql` | `from s3rds import pymysql` | MySQL 在客户端翻译成 SQLite；没有锁、存储过程、触发器 |
| **SQLAlchemy** | `create_engine("sqlite:///app.db")` | `create_engine("sqlite+s3rds://user:pw@URL/app")` | Core 和 ORM 不变；`regexp_match` 被拒绝 |
| **Django** | `"ENGINE": "django.db.backends.sqlite3"` | `"ENGINE": "s3rds.sqlite"` + `HOST` / `USER` / `PASSWORD` | `__regex`、查询里的时区转换、`order_by("?")` 被拒绝；测试跑在 sqlite3 上 |
| **psycopg2** | `import psycopg2` | `from s3rds import psycopg2` | PostgreSQL 在客户端翻译成 SQLite；`.extras` / `.errors` 与 psycopg2 同名；不支持锁、函数、序列、数组；BOOLEAN 返回 1 / 0 |

其余都不变：模型、查询、游标、事务。共同点：同一时刻一个写者，每次提交都上传整个文件（几百毫秒），
数据库保持在 ~20 MB 以内。以后迁 RDS 就是反向同样一处改动（`import pymysql`、`import psycopg2`、`mysql+pymysql://`、
`django.db.backends.mysql`）。被拒绝的功能抛 `NotSupportedError`——绝不悄悄变样。

### 服务端：一次部署，空闲 ≈ $0

你需要在自己的 AWS 账号里有一个 s3rds 服务端——一个 Lambda Function URL 加一个 S3 桶，没人用时什么都不运行。
用 Terraform 或 CloudFormation 部署一次即可（见[快速开始](#快速开始)）。和任何驱动都需要它的数据库一样。

**真·serverless 的 RDS：Lambda URL 就是数据库 endpoint，S3 上的 SQLite 就是存储，闲时 ≈ $0。**

[English](README.md) · *Redefine Serverless* 系列 —— 用 AWS 原语重做那些"闲着也要钱"的托管服务。

| | **s3rds** | RDS `db.t4g.micro` 单 AZ | RDS `db.t4g.micro` Multi-AZ | Aurora Serverless v2 |
|---|---|---|---|---|
| 闲时月费（us-east-2） | **≈ $0**（只有 S3 存储） | ~$14 | ~$28 | ~$45（0.5 ACU 底价） |
| 可用区 | 区域级，天然多 AZ | 一个 | 两个 | 多 |
| 实例故障 | 没有实例 | 停机数分钟 | 故障转移 ~60 秒 | 故障转移 |
| 维护窗口 | 无 | 有，会重启 | 有 | 有 |
| SLA | Lambda 99.95% · S3 99.9% | 单 AZ 无 SLA | 99.95% | 99.99% |
| 持久性 | S3，11 个 9 | EBS | EBS 复制 | 6 副本 |
| SQL 兼容性 | SQLite 方言、经 `s3rds.pymysql` 的 MySQL 方言、经 `s3rds.psycopg2` 的 PostgreSQL 方言、经 `sqlite+s3rds://` 的 SQLAlchemy Core / ORM、经 `ENGINE: "s3rds.sqlite"` 的 Django ORM——见自动生成的[兼容性报告](tests/compat/REPORT.md) | MySQL | MySQL | MySQL / PostgreSQL |

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
| 用户名 / 密码 | HTTP Basic（`console_auth_username` / `console_auth_password`） |
| 数据目录 | `s3://<bucket>/db/` |
| `CREATE DATABASE` | `POST /api/databases` |

## 快速开始

### 1. 部署服务端（二选一）

**Terraform**

```bash
cp terraform/example.json terraform/my.json      # 改 aws_profile、env、console_auth_password
terraform -chdir=terraform init
terraform -chdir=terraform apply -var-file=my.json
terraform -chdir=terraform output                 # db_url、db_ro_url
terraform -chdir=terraform destroy -var-file=my.json   # 全部拆掉（桶要先清空）
```

**CloudFormation**（只需要 AWS CLI）

```bash
cp cloudformation/example-params.json cloudformation/params.json   # 改密码
python3 scripts/build_server.py "s3rds[server]==0.3.1"          # 服务端加上固定版本的 SQLite 引擎
aws cloudformation package --template-file cloudformation/template.yaml \
    --s3-bucket <你自己任意一个桶> --output-template-file /tmp/s3rds.yaml
aws cloudformation deploy --template-file /tmp/s3rds.yaml --stack-name s3rds \
    --capabilities CAPABILITY_IAM --parameter-overrides file://cloudformation/params.json
```

两种方式建出来的东西一样：一个 S3 桶（`<env>-srds-db-2026`）、两个 Lambda（读写、只读）
和它们的公网 Function URL、一个最小权限 IAM role。S3 桶名全球唯一，被占了就改后缀。

### 2. 打开控制台

浏览器打开 `db_url`，用 `console_auth_username` / `console_auth_password` 登录：建库、建表、改行、跑 SQL。
每次操作都显示服务端耗时，以及是命中 `/tmp` 缓存还是去了 S3。

![s3rds 控制台](docs/console.jpg)

### 3. 程序连接

```bash
pip install s3rds
```

```python
from s3rds import sqlite       # SQLite 方言
from s3rds import pymysql      # MySQL 方言             以后上 RDS：import pymysql
from s3rds import psycopg2     # PostgreSQL 方言        以后：import psycopg2

conn = pymysql.connect(host="xxxx.lambda-url.us-east-2.on.aws", user="admin", password="...", database="blog")
with conn.cursor() as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())

# SQLAlchemy（Core / ORM）：只改 URL
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")

# Django：只改 ENGINE 和连接参数
DATABASES = {"default": {"ENGINE": "s3rds.sqlite", "NAME": "blog",
                         "HOST": "xxxx.lambda-url.us-east-2.on.aws", "USER": "admin", "PASSWORD": "..."}}
```

其他语言：就是 HTTPS + JSON。

```bash
curl -u admin:密码 -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query
```

每种方言逐项支持情况：[tests/compat/REPORT.md](tests/compat/REPORT.md)。

## 兼容性

兼容性矩阵是自动生成的，不是手写的：**[tests/compat/REPORT.md](tests/compat/REPORT.md)**。
每一行是一项能力（DDL、DML、查询、函数、约束、元数据、类型），用同一条 SQL 在参照库——`s3rds.pymysql` 对
Docker 里的 MySQL 8.0，`s3rds.psycopg2` 对 Docker 里的 PostgreSQL 16，`s3rds.sqlite` 对标准库 `sqlite3`，`sqlite+s3rds` 对跑在 `sqlite3` 上的 SQLAlchemy，
`ENGINE: "s3rds.sqlite"` 对 Django 自带的 SQLite 后端——和 s3rds 上各跑一遍，比对结果、Python 类型、rowcount、lastrowid 和错误码。

| 状态 | 含义 |
|---|---|
| ✅ verified | 与参照库一致，可放心依赖 |
| ❌ gap | 已知差异，备注写明哪个任务修 |
| ⛔ unsupported by design | 明确拒绝（`NotSupportedError`：行锁、触发器、存储过程），绝不静默走样 |
| 🔴 regression | 以前一致现在不一致，阻止发版 |

重新生成报告（MySQL 参照库跑在 Docker 里）：

```bash
python3 -m pytest tests -q                 # 不碰 Docker：MySQL 在跑就实时比对，否则跳过兼容性测试
python3 -m pytest tests -q --docker up     # 在 Docker 里启动 MySQL，等健康后测试；测完保持运行
python3 -m pytest tests -q --docker run    # 同上，全部通过就关掉；有失败就保持运行并提示
python3 -m pytest tests -q --docker down   # 同上，不管结果一律关掉
python3 -m pytest tests -q --docker run --aws real-srds.json   # Docker 里的 MySQL vs 线上 Lambda（URL 取自 terraform output）
```

### 版本

报告验证时用的版本；`client/pyproject.toml`（`[tool.s3rds.verified]`）里写的是同一份。

| | 版本 | 由谁固定 |
|---|---|---|
| Python（客户端） | 3.9 – 3.13：3.9.6 跑回归测试，3.13.7 跑完整对比 | `requires-python = ">=3.9"` |
| 服务端运行时 | AWS Lambda `python3.12` | `terraform/main.tf` |
| SQLite（服务端引擎） | 3.50.4，随 Lambda 代码包一起部署（运行时自带的是 3.40.0） | extra `pip install "s3rds[server]"` = `sqlean.py==3.50.4.5` |
| sqlglot（MySQL 翻译） | 30.24 | 依赖 `sqlglot>=30.24,<31` |
| SQLAlchemy | 2.0，实测 2.0.55 | 可选 `pip install "s3rds[sqlalchemy]"` = `SQLAlchemy>=2.0,<2.1` |
| Django | 6.1，实测 6.1.2（Python ≥ 3.12） | 可选 `pip install "s3rds[django]"` = `Django>=6.1,<6.2` |
| MySQL（参照库） | Docker 里的 8.0.46 | `tests/compat/docker-compose.yml`：`mysql:8.0.46` |

## 保证

| | 靠什么 |
|---|---|
| **原子性** | 每条请求显式 `BEGIN/COMMIT`（DDL 也在内）+ S3 PUT 本身原子；`/tx` 多条语句一个事务 |
| **一致性** | SQLite 约束；新表默认 `STRICT`；每个请求 `PRAGMA foreign_keys=ON`，除非连接自己关掉（`PRAGMA foreign_keys = OFF`，Django 的迁移会这样做） |
| **隔离性** | 单写者（reserved concurrency 1 + `If-Match` CAS）；读到的是整文件快照；打开的事务（`autocommit=False`）是 serializable：别的写者先提交它就失败，绝不覆盖 |
| **持久性** | S3 PUT 成功后客户端才收到 OK（11 个 9） |
| **绝不重复写** | 驱动只在"请求确定没送达"时重发；超时一律报"结果未知" |
| **可用性** | Lambda 和 S3 是区域级、天然多 AZ 的服务：没有实例会挂、没有 AZ 可丢、没有维护窗口。高于单 AZ 的 RDS 实例（单 AZ 没有 SLA）；RDS 要开 Multi-AZ 花两倍钱才追平 |

**事务，说具体点。** 默认（`autocommit=True`）每条语句在 `execute()` 返回时就已提交并落到 S3。`commit()` 是空操作。
`rollback()` **撤销不了已经执行的语句**——上次 `commit()` 之后有过写入它会发警告，没有就保持安静
（连接池和 ORM 归还连接时会调它）。多条语句要同成同败，用 `conn.transaction([...])`（一次提交、一次 S3 写入）
或 `cursor.executemany(...)`。

`s3rds.sqlite.connect(..., autocommit=False)`（SQLAlchemy 用的就是它）提供 sqlite3 的事务：`INSERT` /
`UPDATE` / `DELETE` 开启事务（两种模式下 `BEGIN` 语句也会开启——Django 的 `atomic()`），事务里的读能看到自己未提交的写，`commit()` 只写一次 S3，`rollback()` 直接丢弃，
`SAVEPOINT` / `ROLLBACK TO` 可用。服务端不保存会话：事务里的每条语句都带上本事务之前的写，服务端在事务开始时的
那个版本上重放、执行新语句、再回滚；`commit()` 再重放一次并真正提交。期间若有别的写者先提交，下一条语句或
`commit()` 抛 `OperationalError`（"transaction conflict"）——回滚后重跑整个事务。代价随事务长度增长（每条语句
都要重放之前的写），事务要短。写入里用了 `CURRENT_TIMESTAMP` 或 `random()` 的，提交时存下的值可能和事务里
看到的略有不同。

**代码该怎么写**（无状态，和 Lambda 一样）：一条语句 = 一次落盘；先读、再决定、最后把该一起成功的写全放进
一个 `transaction([...])` 或一个短的打开事务；能放进 SQL 的判断就放进去（`WHERE balance >= %s`、
`ON DUPLICATE KEY UPDATE`、`INSERT … SELECT`），别跨调用读-改-写；语句写成幂等的，重试才安全。

**大结果集。** 服务端按 4 MB 分页，驱动在 `execute()` 里取完所有页再返回，结果永远完整，绝不静默截断。
翻页期间库被改了，服务端拒绝混合两个版本，驱动自动重跑查询。

这些承诺由测试钉死（内存假 S3 实现了 ETag / `If-Match` / `If-None-Match` 语义）：

```bash
pip install pytest boto3
pytest tests/ -v
```

## 方言

| 驱动 | 你写的 | 不支持（抛 `NotSupportedError`） |
|---|---|---|
| `s3rds.sqlite` | SQLite，不翻译；SQLAlchemy 和 Django 走它 | `create_function`（得在服务端执行） |
| `s3rds.pymysql` | MySQL，在客户端由 sqlglot 翻译成 SQLite；类型和错误码同 pymysql | 存储过程、触发器、`FULLTEXT`、`FOR UPDATE`、`LOCK TABLES`、用户 |
| `s3rds.psycopg2` | PostgreSQL，同样翻译；psycopg2 的 `extras` / `errors` / `extensions` | 函数、触发器、`LISTEN`、`COPY`、锁、序列、数组 |

每个功能都是一条对比 Docker 里真 MySQL 8.0 / PostgreSQL 16 的测试：[tests/compat/REPORT.md](tests/compat/REPORT.md)。
要在 MySQL 上用 ORM？那就是该迁到 RDS 的时候了 —— [MIGRATION.md](MIGRATION.md)。

## 适用规模

最好在 ~20 MB 以内：热读 20–50 ms，写几百 ms，每秒几次写。超过 ~50 MB 迁到 RDS。
适合个人应用、内部工具、博客、MVP；不适合高并发 OLTP。`scripts/bench.py` 可以测你的部署。

## 目录

```
client/           pip 包：s3rds/sqlite/、pymysql.py、psycopg2/（驱动），s3rds/server/（Lambda）
terraform/        部署方式 A
cloudformation/   部署方式 B
tests/            回归 + 兼容性
```

## 发版

测试，改版本号（`client/pyproject.toml`、`__version__`、`terraform/example.json` 的 `server_package`），
写 CHANGELOG，提交，`git tag vX.Y.Z && git push --tags` —— CI 测试、构建、发 PyPI。

## 路线图

待办在 [tests/compat/TASKS.md](tests/compat/TASKS.md)。已完成：打开的事务和 SQLAlchemy（0.2.0）、Django（0.2.1）、`s3rds.psycopg2`（0.3.0）。
下一步：CloudFormation 一键 *Launch Stack* 按钮。

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
