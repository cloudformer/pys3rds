#!/usr/bin/env python3
"""srds 读写延迟/吞吐基准测试(走 pys3mysql 驱动,测业务代码看到的端到端延迟)。

参数全部自动获取,直接跑:
  python3 scripts/bench.py
可选参数(顺序随意,按形状识别):
  xxx.json  指定 var file(默认自动扫 terraform/ 下含 db_user 的 json)
  纯数字    每组次数(默认30)
  其他      库名(默认 benchdb)
例: python3 scripts/bench.py terraform/real-srds.json mt5test 100

自动获取逻辑:
  endpoint   <- terraform output -raw db_url (在 ../terraform 下执行)
  user/pwd   <- var file 里的 db_user / db_password

输出:读/写各自的 冷启动首次、min/p50/p95/max、每秒操作数。
跑完自动 DROP TABLE 清掉自己造的数据;库文件等资源的生命周期归 terraform 管。
提示:第一次请求通常撞上冷启动,单独列出不计入统计;
     想复测"纯冷启动",隔 30 分钟以上再跑一次看首次那行。
"""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TF_DIR = os.path.join(ROOT, "terraform")
sys.path.insert(0, os.path.join(ROOT, "client"))
import pys3mysql  # noqa: E402


def load_conn(varfile=None):
    """endpoint 取自 terraform output,凭证取自 var file(指定的,或自动扫描)。"""
    cfg = None
    if varfile:
        with open(varfile) as fp:
            cfg = json.load(fp)
        if "db_user" not in cfg:
            sys.exit(f"{varfile} 里没有 db_user/db_password")
    else:
        for f in sorted(os.listdir(TF_DIR)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(TF_DIR, f)) as fp:
                        data = json.load(fp)
                except (json.JSONDecodeError, OSError):
                    continue
                if "db_user" in data:
                    cfg = data
                    break
        if not cfg:
            sys.exit("terraform/ 下没找到含 db_user 的 var file(如 real-srds.json)")

    r = subprocess.run(["terraform", "output", "-raw", "db_url"],
                       cwd=TF_DIR, capture_output=True, text=True)
    url = r.stdout.strip()
    if r.returncode != 0 or not url.startswith("http"):
        sys.exit(f"拿不到 db_url(先 terraform apply?):\n{r.stderr.strip()}")
    return url, cfg["db_user"], cfg["db_password"]


def bench(label, fn, n):
    times = []
    for i in range(n):
        t0 = time.perf_counter()
        fn(i)
        times.append((time.perf_counter() - t0) * 1000)
    first, rest = times[0], times[1:] or times
    rest_sorted = sorted(rest)
    p = lambda q: rest_sorted[min(len(rest_sorted) - 1, int(len(rest_sorted) * q))]
    print(f"{label:<14} 首次 {first:7.0f}ms | "
          f"min {min(rest):6.0f}  p50 {p(.5):6.0f}  p95 {p(.95):6.0f}  "
          f"max {max(rest):6.0f}ms | {1000 / statistics.mean(rest):5.1f} 次/秒")


def main():
    varfile, dbname, n = None, "benchdb", 30
    for a in sys.argv[1:]:
        if a.endswith(".json"):
            varfile = a
        elif a.isdigit():
            n = int(a)
        else:
            dbname = a

    url, user, password = load_conn(varfile)
    print(f"endpoint: {url}\n库: {dbname}  用户: {user}  串行 x{n}\n")

    db = pys3mysql.connect(host=url, user=user, password=password, database=dbname)
    db.create_database()
    db.execute("CREATE TABLE IF NOT EXISTS bench "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    size = db.query("SELECT page_count*page_size/1024.0 AS kb "
                    "FROM pragma_page_count(), pragma_page_size()")[0]["kb"]
    print(f"当前库大小: {size:.0f} KB\n")

    bench("读(SELECT)", lambda i: db.query(
        "SELECT COUNT(*) AS n FROM bench"), n)
    bench("写(INSERT)", lambda i: db.execute(
        "INSERT INTO bench (v) VALUES (?)", [f"row-{i}-{'x' * 100}"]), n)
    bench("读(回表查询)", lambda i: db.query(
        "SELECT * FROM bench ORDER BY id DESC LIMIT 10"), n)

    print(f"\n服务端视角(最近一次): {db.last}")
    db.execute("DROP TABLE bench")  # 自己跑的数据自己清
    print("已清理测试数据(DROP TABLE bench)")


if __name__ == "__main__":
    main()
