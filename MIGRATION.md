# srds → RDS MySQL 迁移手册（第一阶段目标）

触发信号（满足任一就启动）：某库逼近几十 MB / 写入每秒多次 / 需要标准驱动直连。
目标形态：RDS MySQL 8.0，db.t4g.micro（us-east-2 约 $12/月 + gp3 存储 $0.08/GB/月）。

两条路线：
- **路线 B（切换日用）**：db lambda 对外 API 不变，内部 `s3_sqlite.py` 换成 pymysql 连 RDS。
  业务方零改动，当天切完。
- **路线 A（稳定后逐个做）**：业务 lambda 直连 RDS，去掉中间一跳。下面的清单以 A 为主。

## 1. Terraform

| 动作 | 内容 |
|---|---|
| 新增 | `aws_db_instance`:engine=`mysql` 8.0, `db.t4g.micro`, `username=var.db_user`, `password=var.db_password`, 开 `backup_retention_period` |
| 新增 | `aws_db_subnet_group` + RDS 安全组（只放行业务 lambda 安全组 → 3306） |
| 修改 | 业务 lambda 加 `vpc_config`（私有子网 + 安全组）——Lambda 必须进 VPC 才能连 RDS |
| 修改 | lambda role 附加 `AWSLambdaVPCAccessExecutionRole`（建 ENI） |
| 修改 | 业务 lambda 环境变量 `DB_ENDPOINT = aws_db_instance.main.address`（原来是 db lambda 的 function_url），`DB_NAME` 不变 |
| 删除 | 业务 lambda 的 `reserved_concurrent_executions = 1`（单写者是 SQLite 的限制，留着卡吞吐） |
| 注意 | 进 VPC 的 lambda 没有公网：只连 RDS 没问题；还要调 S3/外部 API 的业务加 VPC endpoint 或 NAT |

## 2. 依赖打包

```bash
pip install pymysql -t src/<业务>/     # 纯 Python,照旧打 zip,流程不变
```

## 3. 业务代码改动（每个业务约半小时）

| 改动点 | SQLite(srds) | MySQL |
|---|---|---|
| 驱动 | `SrdsClient(endpoint, db, user, pwd)` | `pymysql.connect(host=, database=, user=, password=, autocommit=False)` |
| 连接管理 | 无状态 HTTP | 模块级连接 + 断线重连（`conn.ping(reconnect=True)`,热实例复用 TCP） |
| 占位符 | `?` | `%s` |
| 自增 id | 返回值 `lastrowid` | `cursor.lastrowid`（语义相同,基本不用改逻辑） |
| 事务 | db lambda 自动 commit | 显式 `conn.commit()` |
| 建库建表 | `create_database()` + 冷启动 ensure | 删掉:库 terraform 建,schema 用迁移脚本管,不放业务代码 |

## 4. SQL 方言对照（过一遍 schema 即可，业务 CRUD 基本不动）

| SQLite | MySQL 8.0 |
|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT PRIMARY KEY AUTO_INCREMENT` |
| `TEXT DEFAULT (datetime('now'))` | `DATETIME DEFAULT CURRENT_TIMESTAMP` |
| `TEXT` | 看长度选 `VARCHAR(n)` / `TEXT`;表和库用 `utf8mb4` |
| 动态类型 | 强类型——迁移时脏数据会暴露,提前清 |
| `"标识符"` | 反引号 `` `标识符` ``（或开 ANSI_QUOTES） |

## 5. 数据迁移

```bash
aws s3 cp s3://dev-srds-db-2026/db/<库>.sqlite ./
pip install sqlite3-to-mysql
sqlite3mysql -f <库>.sqlite -d <库名> -u <user> -p -h <rds-endpoint>   # 自动转 schema+数据
```

20MB 分钟级。切换窗口：停写 → 迁 → 两边 `SELECT COUNT(*)` 对账 → 换 `DB_ENDPOINT` → apply。

## 6. 上真数据库后必须升级的周边

- **密码出 tfvars**：`db_password` 挪到 SSM Parameter Store（SecureString），
  terraform/lambda 运行时读取——var file 里放真数据库密码不合格
- **连接数预算**：t4g.micro 的 max_connections 约 60–80;lambda 并发放开后业务多了上 RDS Proxy
- 备份交给 RDS 自动快照；`aws s3 cp` 备份法退役
- db lambda 的归宿：路线 B 期间是代理；全部直连后可退役,或只留作 web 控制台（内部也改连 MySQL）

## 不用动的

业务 SQL 主体、路由逻辑、`terraform apply -var-file=real-srds.json` 的部署流程、
demo 自己 API 的 `auth_token` 门、`DB_ENDPOINT/DB_NAME/DB_USER/DB_PASSWORD` 这套配置形状（只换值）。
