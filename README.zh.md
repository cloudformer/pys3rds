# s3rds

[![PyPI](https://img.shields.io/pypi/v/s3rds)](https://pypi.org/project/s3rds/)
[![Python](https://img.shields.io/pypi/pyversions/s3rds)](https://pypi.org/project/s3rds/)
[![Tests](https://github.com/cloudformer/s3rds/actions/workflows/test.yml/badge.svg)](https://github.com/cloudformer/s3rds/actions/workflows/test.yml)
[![Compatibility](https://img.shields.io/badge/compatibility-report-blue)](tests/compat/REPORT.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) · **[IdleFree](https://cloudformer.github.io/#zh)** 系列 —— 闲时 ≈ $0 的 AWS 原语。

## 概述

在你自己的 AWS 账号里跑一个真·serverless 的 RDS：Lambda Function URL 就是数据库 endpoint，每个数据库是
S3 上的一个 SQLite 文件，两次请求之间什么都不在运行。你的代码保留原来的驱动——今天 `from s3rds import pymysql`，
以后上真 RDS 改回 `import pymysql`。

**适用场景：** 个人应用、内部工具、博客、MVP——小数据库（~20 MB 以内），每秒几次写入。

**费用：** 闲时 ≈ $0（只有 S3 存储）。最便宜的 RDS 约 $14 / 月，Aurora Serverless v2 约 $45。

## 能做什么

- **HTTPS 上的 SQL**：每个 endpoint 一个 Lambda Function URL，HTTP Basic 用户名 / 密码，相当于 RDS 主用户
- **S3 上的 SQLite**：一个库 = 一个 `.sqlite` 对象；读走 `/tmp` 热缓存，写用条件 PUT 传回 S3
- **直接替换的驱动**：`sqlite3`、`pymysql`、`psycopg2`、SQLAlchemy、Django 各自只改一行
- **Web 控制台**：库、表、行和 SQL 输入框，由同一个 Lambda 提供
- **只读 endpoint**：同一份代码，不限并发，适合读多的场景

## 架构

![s3rds 架构](docs/architecture.svg)

**流程：**
1. 驱动把一条 SQL（或一个事务）以 JSON 发给 Function URL
2. Lambda 比对对象的 ETag；没变就直接用 `/tmp` 里的副本，不下载
3. 语句在 SQLite 3.50.4 上的一个事务里执行
4. 只有数据真的变了，文件才用 `If-Match`（比较并交换）传回 S3；冲突就重新下载、重放
5. 返回结果行、`ms`（服务端耗时）和 `s3`（`cache-hit` / `download` / `+upload`）

## 关键特性

- **原子性**：每个请求是一个事务（DDL 也在内）、一次原子的 S3 PUT
- **单写者**：reserved concurrency 1 加 `If-Match` CAS——并发写会重试，绝不覆盖
- **持久性**：S3 存好文件后客户端才收到 OK（11 个 9）
- **绝不重复写**：驱动只在请求确定没送达服务端时才重发
- **绝不悄悄走样**：SQLite 做不到的一律抛 `NotSupportedError`

<details>
<summary>事务细节</summary>

默认（`autocommit=True`）每条语句在 `execute()` 返回时就已提交并落盘。几条语句要一起成功：用
`conn.transaction([...])`（一次提交、一次 S3 写入）或 `executemany(...)`。

`connect(..., autocommit=False)`（SQLAlchemy 和 Django 用的就是它）提供 sqlite3 的事务：事务里的读能看到自己的写，
`commit()` 只写一次 S3，`rollback()` 直接丢弃，savepoint 可用。服务端不保存会话：每条语句都带上本事务之前的写，
由服务端重放。期间若有别的写者先提交，下一条语句抛 `OperationalError`（"transaction conflict"）——重跑即可。
事务要短，代价随长度增长。

大结果集按 4 MB 分页，在 `execute()` 里全部取完；绝不静默截断。
</details>

## 部署

### 前提
- AWS 账号和 CLI 凭证
- Terraform 1.5+
- Python 3 和 pip（Terraform 会把服务端的包装进 Lambda 代码包）

### 部署步骤
```bash
cp terraform/example.json terraform/my.json        # 改 aws_profile、env、console_auth_password（至少 16 位）
terraform -chdir=terraform init
terraform -chdir=terraform apply -var-file=my.json
terraform -chdir=terraform output                   # db_url、db_ro_url
```

会创建一个 S3 桶（`<env>-srds-db-2026`）、读写 Lambda，以及 `"db_ro": true` 时的只读 Lambda，每个都有自己的
Function URL、log group 和 IAM 角色。`terraform -chdir=terraform destroy -var-file=my.json` 会删掉全部，
数据库也一起删（`"force_destroy": true`，默认）；改成 `false`，destroy 遇到非空的桶就停下，数据库保留。

### 安全
- **公网 URL，密码在里面**：`function_url_auth = "NONE"` 时 Function URL 是公网的，所以它的资源策略写
  `principal = "*"`——这是 AWS 对公网 URL 的要求，它只让请求*到达* Lambda。之后每个请求都要通过 HTTP Basic
  （`console_auth_username` / `console_auth_password`），否则 401。`function_url_auth = "AWS_IAM"` 时 AWS 先校验
  SigV4 签名（浏览器控制台签不了名，这种模式下用不了控制台）。
- **没有账号就不放行**：服务端没配用户名和密码时，所有 API 请求返回 503，绝不退回成不认证。密码至少 16 位。
- **一个请求，一个数据库**：`ATTACH`、`DETACH`、`VACUUM INTO` 被拒绝（403），发给一个库的 SQL 打不开别的库的文件，
  也写不了 Lambda 上的文件。
- **没有 CORS**：控制台由同一个 URL 提供，驱动也不是浏览器。
- **S3**：私有桶（公开访问已封）、SSE-S3 加密、桶策略拒绝一切非 TLS 请求。
- **IAM，每个 endpoint 一个角色**：读写角色可以读写 `db/*.sqlite`；只读角色只能读，所以只读 endpoint 即使代码有
  bug 也写不了 S3。两个角色都能列出 `db/`、写自己的 log group——除此之外什么都不行。
- **日志**：每个 Lambda 一个 log group，保留 14 天。
- **关掉控制台**：`"console": false` 只留 API（`GET /` 返回 404）。

## 使用

### 控制台

浏览器打开 `db_url`，用 `console_auth_username` / `console_auth_password` 登录。

![s3rds 控制台](docs/console.jpg)

### 程序连接

```bash
pip install s3rds                # s3rds.sqlite、SQLAlchemy（sqlite+s3rds://）、Django（ENGINE s3rds.sqlite）：没有依赖
pip install "s3rds[mysql]"       # + s3rds.pymysql      （带上 sqlglot，MySQL -> SQLite 翻译）
pip install "s3rds[postgres]"    # + s3rds.psycopg2     （PostgreSQL 同理）
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
curl -u admin:password -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query
```

## 与 RDS 对比

| | **s3rds** | RDS `db.t4g.micro` 单 AZ | RDS `db.t4g.micro` Multi-AZ | Aurora Serverless v2 |
|---|---|---|---|---|
| 闲时月费（us-east-2） | **≈ $0**（只有 S3 存储） | ~$14 | ~$28 | ~$45（0.5 ACU 底价） |
| 可用区 | 区域级，天然多 AZ | 一个 | 两个 | 多 |
| 实例故障 | 没有实例 | 停机数分钟 | 故障转移 ~60 秒 | 故障转移 |
| 维护窗口 | 无 | 有，会重启 | 有 | 有 |
| SLA | Lambda 99.95% · S3 99.9% | 单 AZ 无 SLA | 99.95% | 99.99% |
| 持久性 | S3，11 个 9 | EBS | EBS 复制 | 6 副本 |
| SQL | SQLite、MySQL、PostgreSQL 方言 | MySQL | MySQL | MySQL / PostgreSQL |

## 限制

大小：最好在 ~20 MB 以内（热读 20–50 ms，写几百 ms）；超过 ~50 MB 迁到 RDS——
[MIGRATION.md](MIGRATION.md)。不适合高并发 OLTP。

## 兼容性

| 你在用 | 类别 | 测过的版本 | 不支持（抛 `NotSupportedError`） |
|---|---|---|---|
| `sqlite3` → `s3rds.sqlite` | **原生**：SQLite 语法，不翻译 | Python 3.9 – 3.13 | `create_function`（得在服务端执行） |
| SQLAlchemy → `sqlite+s3rds://` | **原生**：SQLAlchemy 的 SQLite 方言生成 SQL | SQLAlchemy 2.0 | `regexp_match` |
| Django → `ENGINE "s3rds.sqlite"` | **原生**：Django 的 SQLite 后端生成 SQL | Django 4.2、5.2、6.1 | `__regex`、查询里的时区转换、`order_by("?")`、`MD5` / `SHA*`、`StdDev` / `Variance` |
| `pymysql` → `s3rds.pymysql` | **翻译**：MySQL → SQLite，在客户端（sqlglot） | 对照 MySQL 8.0 | 存储过程、触发器、`FULLTEXT`、`SELECT … FOR UPDATE`、`LOCK TABLES`、用户 |
| `psycopg2` → `s3rds.psycopg2` | **翻译**：PostgreSQL → SQLite，在客户端（sqlglot） | 对照 PostgreSQL 16 | 函数、触发器、`LISTEN`、`COPY`、锁、序列、数组 |

原生最可靠：SQL 本来就是 SQLite 的。翻译有边界，每一项都列在报告里。
**新项目优先用 SQLAlchemy 或 Django。**

每个功能都是一条测试：同一条 SQL 在参照库——Docker 里的 MySQL 8.0 和 PostgreSQL 16，跑在 SQLite 上的
`sqlite3`、SQLAlchemy、Django——和 s3rds 上各跑一遍，比对结果行、Python 类型、rowcount 和错误码。
CI 跑上面每一种 Python × Django 组合。自动生成的结果，每项功能和每个差异：
**[tests/compat/REPORT.md](tests/compat/REPORT.md)**。

<details>
<summary>验证过的版本</summary>

| | 版本 |
|---|---|
| Python（客户端） | 3.9、3.10、3.11、3.12、3.13（CI） |
| 服务端 | AWS Lambda `python3.12`，SQLite 3.50.4（`pip install "s3rds[server]"`） |
| sqlglot | 30.24，只在 `s3rds[mysql]` / `s3rds[postgres]` 里 |
| SQLAlchemy | 2.0（实测 2.0.55），`pip install "s3rds[sqlalchemy]"` |
| Django | 4.2（Python 3.9、3.10）、5.2（3.11、3.12）、6.1（3.12、3.13），都在 CI 里，`pip install "s3rds[django]"` |
| 参照库 | Docker 里的 MySQL 8.0.46、PostgreSQL 16.4 |
</details>

## 开发

```bash
.venv/bin/python -m pytest tests -q                                      # 回归 + 兼容性
.venv/bin/python -m pytest tests -q --docker run --aws real-srds.json    # + Docker 里的 MySQL / PostgreSQL 对比线上 Lambda
```

目录：`client/` 是 pip 包（驱动 + `s3rds/server/`，即 Lambda）· `terraform/` · `tests/`。
Terraform 部署的是本仓库当前的代码（`./client[server]`），所以 `apply` 跑的就是你测的。
发版：测试，改版本号（`client/pyproject.toml`、`__version__`），写 CHANGELOG，提交，
`git tag vX.Y.Z && git push --tags`。
待办：[tests/compat/TASKS.md](tests/compat/TASKS.md)。

## IdleFree 系列

**[IdleFree](https://cloudformer.github.io/#zh)** 系列之一 —— 把空闲也计费的 AWS 服务和自建应用，重建成空闲 ≈ $0。MIT。

## 协议

MIT
