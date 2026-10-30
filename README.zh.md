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
python3 scripts/build_server.py                                  # src/db 加上固定版本的 SQLite 引擎
aws cloudformation package --template-file cloudformation/template.yaml \
    --s3-bucket <你自己任意一个桶> --output-template-file /tmp/s3rds.yaml
aws cloudformation deploy --template-file /tmp/s3rds.yaml --stack-name s3rds \
    --capabilities CAPABILITY_IAM --parameter-overrides file://cloudformation/params.json
```

两种方式建出来的东西一样：一个 S3 桶（`<env>-srds-db-2026`）、两个 Lambda（读写、只读）
和它们的公网 Function URL、一个最小权限 IAM role。S3 桶名全球唯一，被占了就改后缀。

### 2. 打开控制台

浏览器打开 `db_url`，用 `console_auth_username` / `console_auth_password` 登录。建库、建表（默认 STRICT）、加列、改行、跑 SQL。
每次操作都显示服务端耗时，以及这次是命中 `/tmp` 缓存还是去了 S3。

### 3. 程序连接

```bash
pip install s3rds    # s3rds.sqlite、s3rds.pymysql / s3rds.psycopg2(MySQL / PostgreSQL 由 sqlglot 在客户端翻译)、SQLAlchemy 用的 sqlite+s3rds://、Django 用的 ENGINE s3rds.sqlite
```

想写哪种方言就 import 哪个，API 都是 pymysql 形状；将来上真 RDS 就是最后一次 `import pymysql`：

```python
from s3rds import sqlite                            # SQLite 方言
# from s3rds import pymysql                         # MySQL 方言(见下面「MySQL 方言」)
# from s3rds import psycopg2                        # PostgreSQL 方言，psycopg2 的 API(见「PostgreSQL 方言」)

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

**SQLAlchemy**（Core 和 ORM）：只换连接串。SQL 由 SQLAlchemy 自带的 SQLite 方言生成，不做任何翻译；
底下的标准库 `sqlite3` 换成 `s3rds.sqlite`，事务行为和 sqlite3 一致（见[事务](#保证)）：

```python
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")
# 以后上 RDS:          "mysql+pymysql://admin:...@my-rds.amazonaws.com/blog"
```

本来就支持 SQLite（`DATABASE_URL=sqlite:///...`）的应用，代码不用改。

**Django**：只换 `ENGINE` 和连接参数。SQL 由 Django 自带的 SQLite 后端生成；底下用 `s3rds.sqlite` 替换 `sqlite3`，
`atomic()` 和 savepoint 就是驱动的打开事务，`migrate` 可用（重建表时连接会关掉外键，驱动把这个设置随每个请求发给服务端）：

```python
DATABASES = {"default": {
    "ENGINE": "s3rds.sqlite",                       # 原来是 "django.db.backends.sqlite3"
    "NAME": "blog", "HOST": "xxxx.lambda-url.us-east-2.on.aws", "USER": "admin", "PASSWORD": "...",
}}
# 以后上 RDS:  "ENGINE": "django.db.backends.mysql"，NAME / HOST / USER / PASSWORD 不变
```

先建一次数据库（控制台，或 `s3rds.sqlite.connect(...).create_database()`），再 `manage.py migrate`。
`manage.py test` 请跑在 `django.db.backends.sqlite3` 上（同样的 SQL，本地文件）：在 s3rds 上建测试库会被拒绝。

不可用：注册在连接上的 Python 函数（`create_function`、SQLAlchemy 的 `regexp_match`）——它们得跑在服务端。
Django 的 SQLite 后端会注册约 35 个这样的函数：其中的日期函数（`__year`、`__date`、`Trunc`、`Extract`、`dates()`）
这里改用 SQLite 自带的 `strftime()` 实现，并和原版逐项比对；其余的抛 `NotSupportedError`——查询里的时区转换
（`TIME_ZONE` 保持和连接一致，默认 UTC）、`__regex`、`MD5` / `SHA*`、`LPad` / `RPad` / `Repeat` / `Reverse`、
`order_by("?")`、`StdDev` / `Variance`、查询里的日期运算（`F("dt") + timedelta`、`F("end") - F("start")`）。

只有一个依赖 sqlglot，只有 `s3rds.pymysql` 和 `s3rds.psycopg2` 用到。线协议就是 HTTPS + JSON，任何语言都能接：

```bash
curl -s -u "admin:密码" -H "content-type: application/json" \
     -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     "https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query"
```

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
| SQLite（服务端引擎） | 3.50.4，随 Lambda 代码包一起部署（运行时自带的是 3.40.0） | `src/db/requirements.txt`：`sqlean.py==3.50.4.5` |
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

**适合谁：** 直接写 MySQL 的代码——pymysql 风格的调用、自己的数据访问层。ORM 走它自己的 SQLite 支持
（SQLAlchemy 用 `sqlite+s3rds://`，Django 用 `ENGINE: "s3rds.sqlite"`，见[程序连接](#3-程序连接)）：走 MySQL 方言需要
服务端会话，ORM 的 MySQL 方言连上就会探测 `SHOW VARIABLES`。需要在 MySQL 上用 ORM 的时候就是该迁 RDS 的时候——见 [MIGRATION.md](MIGRATION.md)。

结果类型和 pymysql 一致：`DATETIME` → `datetime`、`DATE` → `date`、`TIME` → `timedelta`、`DECIMAL` /
`SUM` / `AVG` / 整数除法 → `Decimal`、`BLOB` → `bytes`（双向）、`NOW()` → `datetime`。
模拟严格模式：`'abc'` 插进 `INT` 列报 1366。`DESCRIBE` 和 `SHOW CREATE TABLE` 显示你声明的 MySQL 类型
（存在库里一张小表 `_s3rds_columns`）。

**兼容性报告 —— [tests/compat/REPORT.md](tests/compat/REPORT.md)。** 上面每一项能力对应一个测试：同一条 SQL
在 Docker 里的真 MySQL 8.0 和 s3rds 上各跑一遍，比对结果、Python 类型、rowcount、lastrowid 和错误码。
verified 的可以放心用；gap 的标了哪个任务来修。每次运行自动重新生成（命令见上面[兼容性](#兼容性)一节）。

## PostgreSQL 方言（`s3rds.psycopg2`）

用 psycopg2 的 API 写 PostgreSQL，跑在同一个 endpoint 上。和 MySQL 方言一样在客户端翻译（sqlglot，按语句缓存）。
子模块和 psycopg2 同名：`s3rds.psycopg2.extras`（`RealDictCursor`、`DictCursor`、`Json`、`execute_values`）、
`.errors`（`UniqueViolation` 等，带 `pgcode`）、`.extensions`。

```python
from s3rds import psycopg2                       # 以后：import psycopg2
from s3rds.psycopg2.extras import RealDictCursor

conn = psycopg2.connect(host="xxxx.lambda-url.us-east-2.on.aws", dbname="blog", user="admin", password="...")
with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:   # 和 psycopg2 一样 autocommit=False，`with conn` 提交
    cur.execute("CREATE TABLE IF NOT EXISTS posts (id SERIAL PRIMARY KEY, title TEXT NOT NULL)")
    cur.execute("INSERT INTO posts (title) VALUES (%s) RETURNING id", ["hello"])
```

- `%s` / `%(name)s`；`SERIAL` / `IDENTITY` 主键；`RETURNING`；`ON CONFLICT … DO UPDATE / DO NOTHING`；
  `TRUNCATE … RESTART IDENTITY`；`BEGIN` / `START TRANSACTION` / `COMMIT` / `ROLLBACK`
- `NUMERIC` → `Decimal`、`DATE` → `date`、`TIMESTAMP` → `datetime`；报错带 PostgreSQL 的 SQLSTATE（23505、42P01……）
- 已知差距（任务 28、29）：`BOOLEAN` 返回 1 / 0、`LIKE` 不区分大小写、`x / 0` 得 NULL、
  `::timestamp` / `::numeric` / `::text` 转换

不支持的——抛 `NotSupportedError`（`errors.FeatureNotSupported`，0A000）：函数/触发器、`LISTEN` / `NOTIFY`、`COPY`、
`SELECT … FOR UPDATE` / `LOCK`、角色/`GRANT`、序列、数组、扩展、临时表。对照 Docker 里的 PostgreSQL 16，
结果见 [tests/compat/REPORT.md](tests/compat/REPORT.md)。

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
client/           pip 包 s3rds:s3rds/sqlite/(驱动;base.py 是 Django 后端)、s3rds/pymysql.py、s3rds/psycopg2/、pyproject.toml
terraform/        部署方式 A
cloudformation/   部署方式 B
tests/            正确性测试
scripts/          bench.py
MIGRATION.md      长大了怎么迁 RDS MySQL
```

## 发版

CI（`.github/workflows/test.yml`）每次 push 跑全部测试（含 MySQL 对比）。发版就是打 tag：

```bash
# 改 client/pyproject.toml 和 client/s3rds/sqlite/__init__.py 的版本号,写 CHANGELOG,提交,然后
git tag v0.3.0 && git push --tags      # 测试 → 构建 client/ → 发 PyPI(trusted publishing,不用 token)
```

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
