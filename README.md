# serverless-rds-s3 (srds)

> **设计原则**：这是一个关系型数据库，数据正确性第一——原子性、一致性不妥协，
> 写入可以失败，但绝不重复、绝不写错。优势是省钱（闲时 ≈ $0），代价是性能一般。
> 取舍顺序：正确性 > 成本 > 性能。

真·serverless 的 RDS：**一个数据库 = S3 上的一个 `.sqlite` 文件**。
Lambda 被调用时把文件拉到 `/tmp` 用 SQLite 操作，写完整文件回传 S3。
没有请求时成本为 0（只有几分钱的 S3 存储费），Lambda 和请求量都在永久免费额度内。

```
浏览器(web 控制台) ────────────────────────┐
srds-demo-lambda(业务示例) ── pys3mysql ────┼──→ srds-db-lambda ──→ s3://dev-srds-db-2026/db/*.sqlite
ECS / EC2 / 本地脚本 ──────── pys3mysql ────┘    (唯一碰 S3 的进程)    (一个 key 一个库)
```

环境体现在**桶名**里（`dev-srds-db-2026`），桶内目录所有环境一致：
`db/demo.sqlite`、`db/mt5.sqlite`……换环境 = 换桶，路径不变。

两个 Lambda 完全独立（各自目录、各自 zip）：

- **db**：通用 serverless 数据库，相当于 RDS endpoint。对人是 DataGrip 风格 web 控制台
  （选库/选表、分页浏览、行内编辑/删除/插入、SQL 控制台、新建库）；
  对程序是 HTTP SQL API：`POST /api/db/{db}/query {sql, params}` 单条，
  `POST /api/db/{db}/tx {statements:[...]}` 多条一个事务；都支持参数化防注入。
- **demo**：业务 lambda 示例。不直接碰 S3，通过驱动 `pys3mysql`
  （零依赖 HTTP"驱动"，只用标准库 urllib）连 db lambda。
  ECS/EC2/本地脚本接入方式一模一样：拷这一个文件 + endpoint URL + token。
- 所有读写都汇聚到 db lambda（`reserved_concurrency=1`）→ 全局单写者，
  多少个客户端都不会写冲突；客户端不需要任何 AWS 凭证。

对应传统 RDS 的概念：

| 传统 RDS | srds |
|---|---|
| 数据库服务器实例(endpoint) | db lambda(Function URL) |
| 实例里的一个 database | bucket 里的一个 `.sqlite` 文件 |
| 连接串里的 database 参数 | URL 路径 `/api/db/{name}` |
| 用户名/密码 | HTTP Basic（`db_user`/`db_password`，一一对应） |
| 数据目录(datadir) | `s3://<bucket>/db/` |
| `CREATE DATABASE` | `POST /api/databases` |

区别只有一个:这个"实例"不常驻,没请求时不存在,状态全在 S3——
所以待机 ≈ $0,代价是单写者、低吞吐。

闲时费用明细(别被"$0"误导,是约等于):S3 存储 $0.023/GB·月(20MB 的库 ≈ $0.0005);
Lambda 每月 100 万次请求 + 40 万 GB·秒永久免费,超出后 $0.20/百万次;
CloudWatch 日志 $0.50/GB 写入(建议 7 天保留)。没有请求时,除 S3 存储外一分钱不走。

## 数据规范（正确性第一）

- **新表一律 `STRICT`**：`CREATE TABLE t (...) STRICT`，类型不对直接报错，不靠亲和性猜。
  列类型只能是 `INTEGER / REAL / TEXT / BLOB / ANY`。「＋ 新建表」默认就是 STRICT
- **外键默认开启**：服务端每个写连接 `PRAGMA foreign_keys=ON`，声明了 `REFERENCES` 就会被检查
- **多条语句要同成同败就用事务**：`db.transaction([(sql, params), ...])` / `POST /tx`，
  任一条失败整体回滚、S3 不动；`/query` 一次只跑一条
- **每次写入要么完整落到 S3，要么干净失败**：上传失败即作废本地缓存，不留幽灵数据；
  客户端只在"请求确定没送达"时重发一次，超时一律报"结果未知"由调用方决定

## 目录

```
src/
├── db/                     # 通用 serverless 数据库
│   ├── lambda_function.py  #   API:/api/databases、tables、table 浏览、query
│   ├── index.html          #   单文件 UI,无框架无构建
│   └── s3_sqlite.py        #   核心模块(见下)
└── demo/                   # 业务 lambda 示例,库名 demo
    └── lambda_function.py  #   GET/POST /items(打包时自动带上 client/pys3mysql.py)
client/                     # pip 包 pys3mysql:驱动(接口同 pymysql,零依赖)
├── pys3mysql.py            #   单文件,业务方只依赖它;demo 的 zip 也直接取这一份
├── pyproject.toml
└── README.md               #   PyPI 页面
terraform/
├── dev.json                # ← 所有环境配置在这里(bucket 名、profile、token)
├── providers.tf  backend.tf  variables.tf  main.tf  outputs.tf
└── (state 在本地 state/,同 taptag)
```

`s3_sqlite.py` 是整个方案的核心，约 150 行：

- **读**：按 ETag 做 `/tmp` 热缓存，S3 没变化就不重复下载（热实例连续请求 ~20ms）
- **写**：整文件回传，带 `If-Match` 条件写（CAS）；412 冲突自动重拉重放
- 另外 terraform 给每个 Lambda 设了 `reserved_concurrent_executions = 1`，
  物理上单写者，CAS 只是第二道保险

## 部署

区域和账号都在 `terraform/real-srds.json`：

- `region`：`us-east-2`，bucket 和 lambda 都建在这个 region（S3 桶是 regional 资源，
  只是名字全局唯一）
- `aws_profile`：`dev`（同 taptag），账号就是这个 profile 指向的账号，
  部署前可 `aws sts get-caller-identity --profile dev` 确认
- `db_user` / `db_password`：数据库凭证，语义 = RDS 的 user/password，走 HTTP Basic。
  web 控制台登录、`pys3mysql.connect()`、curl `-u` 都用它。密码改成随机串
- `auth_token`：demo 业务 API 自己的门（`x-auth-token` 头），与数据库凭证无关，也换掉

桶名不用配，规则写死在 `main.tf`：`{env}-srds-db-2026`（dev → `dev-srds-db-2026`）。
S3 桶名全局唯一，万一被占了改 `main.tf` 里 `local.bucket` 的后缀。

所有 terraform 命令集中在这（都在 `terraform/` 目录下执行）：

```bash
cd terraform
terraform init                                    # 首次执行一次
terraform plan     -var-file=real-srds.json       # 预览改动
terraform apply    -var-file=real-srds.json       # 部署/更新
terraform output                                  # 看 URL(-raw db_url / demo_url)

terraform plan -destroy -var-file=real-srds.json  # 预览销毁
terraform destroy       -var-file=real-srds.json  # 销毁整套(桶里数据一起删!)
```

改了 `src/` 下的代码、`real-srds.json` 里的 token 等任何东西，都是再跑一次
`terraform apply -var-file=real-srds.json`（archive_file 自动重新打 zip，hash 变了才更新函数）。

## 使用

**web 控制台**（通用数据库）：浏览器打开 `terraform output -raw db_url`，
输入数据库用户名/密码（`db_user`/`db_password`）→ 新建库 → ＋表/＋列 → 网格里直接增删改查。

**demo 业务服务**：

```bash
DEMO=$(terraform output -raw demo_url)
TOKEN=你的token

curl -s -H "x-auth-token: $TOKEN" ${DEMO}items
curl -s -X POST -H "x-auth-token: $TOKEN" -H "content-type: application/json" \
     -d '{"text":"hello srds"}' ${DEMO}items
```

写入后回到 web 控制台刷新，能看到 `demo` 库和 `items` 表——同一份数据。

## 加一个新业务

1. `cp -r src/demo src/blog`，改 `lambda_function.py` 的路由和 schema
2. `main.tf` 里照抄 demo 的 `archive_file` + `aws_lambda_function` + URL 三件套，
   环境变量 `SRDS_DB` 换成 `"blog"`
3. `terraform apply -var-file=real-srds.json`

不是 Lambda 也一样接——ECS/EC2/本地脚本 `pip install pys3mysql`（或拷 `client/pys3mysql.py`），
接口和 `pymysql` 一个样，迁 RDS 时只改 `import`：

```python
import pys3mysql                                   # 将来: import pymysql

conn = pys3mysql.connect(host="xxx.lambda-url.us-east-2.on.aws",
                         user="admin", password="...", database="blog")
with conn.cursor() as cur:                          # pymysql 写法原样能跑
    cur.execute("INSERT INTO t (a) VALUES (%s)", ["hi"])
    print(cur.lastrowid)
with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM t WHERE a = %s", ["hi"])
    print(cur.fetchall())

conn.query("SELECT * FROM t")                       # 便捷方法 → [dict, ...]
conn.transaction([("UPDATE t SET a = %s", ["x"]), ("INSERT INTO t (a) VALUES (%s)", ["y"])])
# 出错统一 except pys3mysql.Error;占位符 %s 和 ? 都认
```

发布到 PyPI（在 `client/` 下）：`pip install build twine && python -m build && twine upload dist/*`

或者直接 curl（这就是"线协议"，任何语言都能接）：

```bash
curl -s -u "admin:数据库密码" -H "content-type: application/json" \
     -d '{"sql":"SELECT * FROM items WHERE id = ?","params":[1]}' \
     "$(terraform output -raw db_url)api/db/demo/query"
```

## 测试

`tests/` 用内存假 S3（实现了 ETag / If-Match / If-None-Match 语义）直接验证正确性承诺：
写失败回滚且不上传、DDL 也在事务内、并发冲突重放不覆盖、重试耗尽干净失败、
上传失败不留幽灵数据、残留 journal 被清理、只读连接拒绝写、外键生效、
客户端只在"确定没送达"时重发、事务接口全成或全败、只读入口 403。

```bash
pip install pytest boto3      # 一次性
pytest tests/ -v
```

## 适用规模（按单个库的文件大小）

python3 ../scripts/bench.py terraform/real-srds.json test1-db 20

| 库大小 | 结论 |
|---|---|
| **< 10MB** | 甜点区,放心用:热读 20–50ms,冷读/写 ~300ms,完全无感知 |
| **10–20MB** | 设计极限,正常用:写入进入几百 ms,手感仍流畅 |
| **20–50MB** | 能用,但性价比开始下降:每次写整文件回传,冷读/写 ~1s,写频繁会烦 |
| **> 50MB** | 换 RDS:按 `MIGRATION.md` 迁 RDS MySQL(db.t4g.micro),别硬撑 |

原理:ETag 缓存让日常读不碰文件,大小只影响冷读和每次写的整文件传输,
所以上限由"写入延迟能忍到多少"决定。

## 其他限制（自用场景下都无所谓）

- 写吞吐 ~几次/秒；`reserved_concurrency=1` 下并发请求排队
- Function URL 是公网地址，安全完全靠 `auth_token`，别用弱口令
- `terraform destroy` 会连 bucket 里的数据一起删（`force_destroy=true`），
  要留的数据先 `aws s3 cp` 备份

## 删除/清理

按破坏力从小到大：

**删表**：web 控制台 SQL 里跑（删数据不删表用 `DELETE FROM`，清空重置自增用 `DROP` 再建）

```sql
DROP TABLE notes
```

**删一个库**：不提供删库操作（防手滑），UI 也没有删库按钮；
库里的东西用 `DROP TABLE` 清，环境级删除统一走 terraform destroy。

**备份一个库**（定期,或删之前）：

```bash
aws s3 cp s3://dev-srds-db-2026/db/demo.sqlite ~/backup/demo-$(date +%F).sqlite --profile dev
# 恢复就是反向 cp 回去
```

**看现在有哪些库**：web 控制台顶栏的库下拉框。

**销毁整套**（lambda、bucket、IAM 全删；`force_destroy=true` 会连桶里所有数据一起删，
要留的先备份）——命令在上面「部署」一节的速查块里。
