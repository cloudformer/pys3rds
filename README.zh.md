# pys3rds

**真·serverless 的 RDS：Lambda URL 就是数据库 endpoint，S3 上的 SQLite 就是存储，闲时 ≈ $0。**

[English](README.md) · *Redefine Serverless* 系列 —— 用 AWS 原语重做那些"闲着也要钱"的托管服务。

![pys3rds 架构](docs/architecture.svg)

> 设计原则：这是一个关系型数据库，数据正确性第一——原子性、一致性不妥协，
> 写入可以失败，但绝不重复、绝不写错。优势是省钱（闲时 ≈ $0），代价是性能一般。
> 取舍顺序：正确性 > 成本 > 性能。

## 为什么

AWS 的"serverless"大多指**实例会自动伸缩**，很少指**根本没有实例**。

| 方案 | 闲时月费（us-east-2，约数） |
|---|---|
| **pys3rds** | **≈ $0** —— 只有 S3 存储费，20 MB 的库 ≈ $0.0005 |
| RDS MySQL `db.t4g.micro`（最便宜的 provisioned） | ~$14（实例 + 20 GB gp3；免费层只有 12 个月） |
| RDS MySQL `db.t4g.medium` | ~$50 |
| Aurora Serverless v2（最低 0.5 ACU） | ~$45（暂停了存储也照收） |

pys3rds 没有实例，两次请求之间什么都不在运行。Lambda 在永久免费额度内
（每月 100 万次请求 + 40 万 GB·秒），超出后 $0.20/百万次。

## 怎么工作

- **一个库 = S3 上一个 `.sqlite` 文件。** db lambda 把它拉到 `/tmp`、执行 SQL，**只有数据真的变了**才整文件回传
  （SQLite authorizer + 文件指纹双重判定）。
- **读很便宜**：对着 `/tmp` 热缓存比一下 ETag，服务端 ~15 ms，不下载。
- **写很安全**：每条请求一个显式事务（DDL 也在内）；上传是 `If-Match` 条件写（CAS）。冲突就重拉重放，
  重试耗尽就干净失败并作废本地副本。永远没有幽灵数据。
- **天然单写者**（`reserved_concurrency = 1`），另有一个**只读 endpoint**不限并发（同一份代码，`READONLY=1`）。

对应 RDS 的概念：

| RDS | pys3rds |
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
cp client/pys3mysql.py src/demo/                                   # demo 要带上驱动
aws cloudformation package --template-file cloudformation/template.yaml \
    --s3-bucket <你自己任意一个桶> --output-template-file /tmp/pys3rds.yaml
aws cloudformation deploy --template-file /tmp/pys3rds.yaml --stack-name pys3rds \
    --capabilities CAPABILITY_IAM --parameter-overrides file://cloudformation/params.json
```

两种方式建出来的东西一样：一个 S3 桶（`<env>-srds-db-2026`）、三个 Lambda（读写、只读、demo）
和它们的公网 Function URL、一个最小权限 IAM role。S3 桶名全球唯一，被占了就改后缀。

### 2. 打开控制台

浏览器打开 `db_url`，用 `db_user` / `db_password` 登录。建库、建表（默认 STRICT）、加列、改行、跑 SQL。
每次操作都显示服务端耗时，以及这次是命中 `/tmp` 缓存还是去了 S3。

### 3. 程序连接

```bash
pip install pys3rds
```

```python
import pys3mysql                                    # 将来: import pymysql,其他一个字不改

conn = pys3mysql.connect(host="xxxx.lambda-url.us-east-2.on.aws",
                         user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
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

## 保证

| | 靠什么 |
|---|---|
| **原子性** | 每条请求显式 `BEGIN/COMMIT`（DDL 也在内）+ S3 PUT 本身原子；`/tx` 多条语句一个事务 |
| **一致性** | SQLite 约束；新表默认 `STRICT`；每个写连接 `PRAGMA foreign_keys=ON` |
| **隔离性** | 单写者（reserved concurrency 1 + `If-Match` CAS）；读到的是整文件快照 |
| **持久性** | S3 PUT 成功后客户端才收到 OK（11 个 9） |
| **绝不重复写** | 驱动只在"请求确定没送达"时重发；超时一律报"结果未知" |

这些承诺由测试钉死（内存假 S3 实现了 ETag / `If-Match` / `If-None-Match` 语义）：

```bash
pip install pytest boto3
pytest tests/ -v
```

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
client/           pip 包 pys3rds:pys3mysql.py(驱动)、pyproject.toml
terraform/        部署方式 A
cloudformation/   部署方式 B
tests/            正确性测试
scripts/          bench.py
MIGRATION.md      长大了怎么迁 RDS MySQL
```

## 路线图

- `pys3mysql` 0.2：MySQL 方言兼容层（`AUTO_INCREMENT`、`NOW()`、`INSERT IGNORE`、
  `ON DUPLICATE KEY UPDATE`、`SHOW TABLES`、MySQL 错误码），用 Docker 里的真 MySQL 对照验证。
  不支持的特性抛 `NotSupportedError`，绝不静默出错。
- `pys3pgsql`：psycopg2 形状的驱动。
- CloudFormation 一键 *Launch Stack* 按钮。

## 协议

MIT
