# -*- coding: utf-8 -*-
"""迁移打包：把整套系统打成可拷到另一台机器的压缩包。

用法：
    python -m stock_system.tools.make_bundle              # 只带表结构 + 配置数据
    python -m stock_system.tools.make_bundle --with-data  # 连行情/财务数据一起带

两种模式的区别：
- 默认（轻量）：带 22 张表结构 + dim_*/sys_* 等小表（股票列表、交易日历、策略、自选股）。
  行情、财务、指标这些大表到新机器上重新采集即可，不必传 1.7GB。
- --with-data：把 fact_daily_quote / fact_daily_quote_adj / fact_indicator_daily /
  fact_financial 也导出来，约 1.7GB，压缩后几百 MB，适合网络快或不想等采集的场景。
"""
import argparse
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../stock_system
PROJECT_DIR = os.path.dirname(PKG_DIR)                                   # D:\Stocks\选股系统

# 需要带入的小表（结构之外的数据）
SMALL_TABLES = [
    "dim_stock", "dim_trade_calendar", "dim_strategy", "dim_watchlist",
    "dim_industry", "dim_concept", "dim_index",
    "sys_task_log", "sys_data_quality",
    "fact_screen_run", "fact_screen_result",
]
# 大表：仅在 --with-data 时导出
BIG_TABLES = [
    "fact_daily_quote", "fact_daily_quote_adj",
    "fact_indicator_daily", "fact_financial",
]
REQUIRED_PKGS = [
    "fastapi", "uvicorn", "SQLAlchemy", "PyMySQL", "pandas",
    "akshare", "APScheduler", "openpyxl",
]

SKIP_DIRS = {"__pycache__", ".pytest_cache", "logs", "dist", "_disabled", "exports"}
SKIP_EXT = {".pyc", ".pyo", ".log"}


def find_mysqldump():
    """定位 mysqldump：环境变量优先，其次常见安装路径。"""
    env = os.getenv("STOCK_MYSQL_BIN")
    if env and os.path.isfile(os.path.join(env, "mysqldump.exe")):
        return os.path.join(env, "mysqldump.exe")
    for base in (r"D:\Stocks\mysql\mysql-8.0.29-winx64\bin",
                 r"C:\Program Files\MySQL\MySQL Server 8.0\bin"):
        p = os.path.join(base, "mysqldump.exe")
        if os.path.isfile(p):
            return p
    found = shutil.which("mysqldump")
    return found


def dump_sql(out_dir, dump_exe, with_data):
    """导出 SQL。返回生成的文件名列表。"""
    from .. import config

    os.makedirs(out_dir, exist_ok=True)
    base = [
        dump_exe,
        f"--host={config.DB_HOST}", f"--port={config.DB_PORT}",
        f"--user={config.DB_USER}",
        "--default-character-set=utf8mb4",
        "--single-transaction", "--quick", "--no-tablespaces",
    ]
    # 密码走环境变量，避免出现在命令行历史与告警里
    env = dict(os.environ, MYSQL_PWD=config.DB_PASSWORD)

    def run_to(path, header, extra):
        """先写头部再追加 mysqldump 输出。

        必须先 flush：子进程直接写同一个文件描述符，Python 的缓冲内容
        如果没落盘，会被子进程从偏移 0 处覆盖掉（头部就丢了）。
        """
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(header)
            f.flush()
            subprocess.run(base + extra, stdout=f, check=True, text=True, env=env)

    files = []

    # 1) 建库 + 表结构
    schema_path = os.path.join(out_dir, "01_schema.sql")
    run_to(
        schema_path,
        f"-- 建库与建表\n"
        f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
        f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
        f"USE `{config.DB_NAME}`;\n\n",
        ["--no-data", "--skip-add-drop-table", config.DB_NAME],
    )
    files.append(schema_path)

    # 2) 小表数据
    small_path = os.path.join(out_dir, "02_config_data.sql")
    run_to(
        small_path,
        f"USE `{config.DB_NAME}`;\n\n",
        ["--no-create-info", "--complete-insert", "--skip-add-locks",
         config.DB_NAME] + SMALL_TABLES,
    )
    files.append(small_path)

    # 3) 大表数据（可选）
    if with_data:
        fact_path = os.path.join(out_dir, "03_fact_data.sql")
        run_to(
            fact_path,
            "-- 大表数据（--skip-extended-insert：每行一条 INSERT，避免单条超长 INSERT\n"
            "-- 在导入端触发 max_allowed_packet / ERROR 2013）。导入前可执行\n"
            "-- SET unique_checks=0; SET foreign_key_checks=0; 提速。\n"
            f"USE `{config.DB_NAME}`;\n\n",
            # 关键：--skip-extended-insert 让每张表导出为「每行一条 INSERT」，
            # 单条包极小，跨机器导入永远不会撞 max_allowed_packet 上限。
            ["--no-create-info", "--complete-insert", "--skip-extended-insert",
             "--skip-add-locks", config.DB_NAME] + BIG_TABLES,
        )
        files.append(fact_path)

    return files


CONFIG_BAT = r"""@echo off
rem ===== 迁移后请按新机器的实际情况修改这三行 =====
set "PY=C:\Python313\python.exe"
set "MYSQL_HOME=D:\Stocks\mysql\mysql-8.0.29-winx64"
set "MY_CNF=D:\Stocks\mysql\my.ini"
rem ==============================================
"""

START_BAT = r"""@echo off
chcp 936 >nul
title Stock System - Start
call "%~dp0config.bat"

echo ==========================================
echo   A-Share Stock System - Start
echo ==========================================
echo.

netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   MySQL already running
    goto :web
)
echo   starting MySQL ...
start "MySQL 8.0" /min "%MYSQL_HOME%\bin\mysqld.exe" --defaults-file="%MY_CNF%"
set /a n=0
:wait_db
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if not errorlevel 1 goto :web
set /a n+=1
if %n% geq 40 goto :dbfail
ping -n 2 127.0.0.1 >nul
goto :wait_db
:dbfail
echo   [ERROR] MySQL did not start in 40s
pause
exit /b 1

:web
echo   MySQL OK
netstat -ano | findstr ":8800" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   Web already running
    goto :open
)
echo   starting Web ...
start "Stock Web" /min /d "%~dp0." "%PY%" -m uvicorn stock_system.api.main:app --host 127.0.0.1 --port 8800
set /a n=0
:wait_web
netstat -ano | findstr ":8800" | findstr "LISTENING" >nul
if not errorlevel 1 goto :open
set /a n+=1
if %n% geq 30 goto :webfail
ping -n 2 127.0.0.1 >nul
goto :wait_web
:webfail
echo   [ERROR] Web did not start in 30s
pause
exit /b 1

:open
echo.
echo   Ready: http://127.0.0.1:8800/
start "" http://127.0.0.1:8800/
"""

STOP_BAT = r"""@echo off
chcp 936 >nul
title Stock System - Stop
call "%~dp0config.bat"
"%PY%" "%~dp0service.py" stop
echo.
pause
"""


def write_scripts(code_dir):
    for name, text in (
        ("config.bat", CONFIG_BAT),
        ("启动服务.bat", START_BAT),
        ("停止服务.bat", STOP_BAT),
    ):
        # 批处理由 cmd 按系统代码页读取（中文 Windows 为 GBK），
        # 用 UTF-8 写注释里的中文会变乱码，这里统一写 GBK。
        with open(os.path.join(code_dir, name), "w", encoding="gbk", newline="\r\n") as f:
            f.write(text)


def write_requirements(code_dir):
    lines = []
    for pkg in REQUIRED_PKGS:
        try:
            lines.append(f"{pkg}=={md.version(pkg)}")
        except md.PackageNotFoundError:
            lines.append(pkg)
    with open(os.path.join(code_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("# 与打包机器一致的版本，先装这些再启动\n")
        f.write("\n".join(lines) + "\n")
    return lines


def write_guide(root, with_data, db_size_mb):
    mode = "完整数据" if with_data else "结构 + 配置数据"
    text = f"""# 迁移部署说明

本包由 `make_bundle.py` 于 {datetime.now():%Y-%m-%d %H:%M} 生成，数据模式：**{mode}**。

## 包内结构

```
code/                程序代码（stock_system 包 + 启动脚本 + requirements.txt）
db/                  01_schema.sql（建库建表）
                     02_config_data.sql（股票列表/日历/策略/自选股等小表）
                     {'03_fact_data.sql（行情/财务/指标大表）' if with_data else '（未包含行情等大表，需在新机器重新采集）'}
部署说明.md          本文件
```

## 目标机器要求

- Windows 10/11
- Python 3.13（其它 3.10+ 版本也可，需重新装依赖）
- MySQL 8.0（务必 8.0+，建表用了 `utf8mb4_0900_ai_ci` 与 JSON 列）

## 步骤

### 1. 放置代码

把 `code/` 整个目录拷到目标机器，例如 `D:\\Stocks\\选股系统`。
目录下应能直接看到 `stock_system\\`、`service.py`、`config.bat`。

### 2. 安装 Python 依赖

```bat
cd /d D:\\Stocks\\选股系统
python -m pip install -r requirements.txt
```

### 3. 安装并初始化 MySQL 8.0

若目标机器没有 MySQL，用免安装 ZIP 版最省事（无需管理员权限）：

1. 下载 `mysql-8.0.29-winx64.zip`，解压到 `D:\\Stocks\\mysql\\mysql-8.0.29-winx64`
2. 在 `D:\\Stocks\\mysql\\my.ini` 写配置（参考下方）
3. 初始化数据目录：

```bat
D:\\Stocks\\mysql\\mysql-8.0.29-winx64\\bin\\mysqld.exe --defaults-file=D:\\Stocks\\mysql\\my.ini --initialize-insecure --console
```

4. 启动（新开一个窗口，保持不关）：

```bat
D:\\Stocks\\mysql\\mysql-8.0.29-winx64\\bin\\mysqld.exe --defaults-file=D:\\Stocks\\mysql\\my.ini
```

`my.ini` 参考：

```ini
[mysqld]
port=3306
basedir=D:/Stocks/mysql/mysql-8.0.29-winx64
datadir=D:/Stocks/mysql/data
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
default-time-zone=+08:00
max_connections=200
innodb_buffer_pool_size=1G
local_infile=1
log_bin=mysql-bin
binlog_expire_logs_seconds=604800

[client]
default-character-set=utf8mb4
```

### 4. 建账号

```sql
CREATE USER IF NOT EXISTS 'stock_app'@'localhost' IDENTIFIED BY 'StockSys_2026';
GRANT ALL PRIVILEGES ON stock_system.* TO 'stock_app'@'localhost';
FLUSH PRIVILEGES;
```

### 5. 导入数据（按顺序）

```bat
cd /d D:\\Stocks\\选股系统
mysql -u root -p --default-character-set=utf8mb4 < db\\01_schema.sql
mysql -u root -p --default-character-set=utf8mb4 < db\\02_config_data.sql
{'mysql -u root -p --default-character-set=utf8mb4 < db\\03_fact_data.sql' if with_data else '（无大表数据，跳过）'}
```

> 导入大表时先执行 `SET unique_checks=0; SET foreign_key_checks=0;` 可快数倍，
> 导入结束后再 `SET unique_checks=1; SET foreign_key_checks=1;`。

### 6. 改启动脚本里的路径

打开 `config.bat`，把三行改成目标机器的实际路径：

```bat
set "PY=C:\\Python313\\python.exe"
set "MYSQL_HOME=D:\\Stocks\\mysql\\mysql-8.0.29-winx64"
set "MY_CNF=D:\\Stocks\\mysql\\my.ini"
```

> `PY` 必须是装了依赖的那个解释器。不确定就用 `where python` 查，或直接写
> `python`（前提是它在 PATH 里）。

### 7. 启动并验证

双击 `启动服务.bat`，它会依次拉起 MySQL 和 Web，最后打开浏览器。

手工验证：

```bat
curl http://127.0.0.1:8800/health
```

应返回 `{{"status":"ok"}}`。页面打开后底部显示「数据服务正常」即成功。

## {'数据已随包迁移' if with_data else '如果没带数据，需要重新采集'}

{'' if with_data else '''打开 `http://127.0.0.1:8800/#data`，依次点：

1. **同步**（股票列表）—— 约 30 秒
2. **补齐**（股本）—— 约 2 分钟
3. **补齐历史**（起始日选 2024-01-01）—— 约 60 分钟
4. **同步**（财务数据）—— 约 30 分钟（完成后自动重算指标）

也可以命令行：

```bat
python -m stock_system.scripts.sync --task base
python -m stock_system.scripts.sync --task quote --start 20240101
python -m stock_system.scripts.sync --task financial
python -m stock_system.scripts.sync --task indicator
```
'''}

## 数据库连接参数

代码读环境变量，默认值如下（与源机器一致）：

| 变量 | 默认值 |
|---|---|
| `STOCK_DB_HOST` | 127.0.0.1 |
| `STOCK_DB_PORT` | 3306 |
| `STOCK_DB_USER` | stock_app |
| `STOCK_DB_PASSWORD` | StockSys_2026 |
| `STOCK_DB_NAME` | stock_system |

新机器若改了账号密码，两种改法任选：

1. 改 `stock_system/config.py` 里的默认值
2. 设系统环境变量（`setx STOCK_DB_PASSWORD 新密码`），代码优先读环境变量

## 常见问题

**页面空白 / 接口 500**：先看 `http://127.0.0.1:8800/health` 是否通；不通说明 Web 没起来，看窗口报错。通了但页面空，多半是数据库连不上——检查 MySQL 是否在 3306 监听、账号密码是否正确。

**启动脚本闪退**：在 cmd 里手动执行一遍，能看到具体报错。常见是 `PY` 路径写错，或依赖没装全。

**时间显示不对**：MySQL 的 `default-time-zone` 要设 `+08:00`，否则时间戳差 8 小时。

**当天数据没更新**：每日 16:00 有定时任务，但要求 MySQL 和 Web 服务都在运行。服务没起，定时任务不会执行。
"""
    path = os.path.join(root, "部署说明.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def copy_code(code_dir):
    def ignore(_dir, names):
        return [n for n in names
                if n in SKIP_DIRS or os.path.splitext(n)[1] in SKIP_EXT]

    shutil.copytree(PKG_DIR, os.path.join(code_dir, "stock_system"), ignore=ignore)

    for name in ("service.py", "README.md"):
        src = os.path.join(PROJECT_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, code_dir)
    readme = os.path.join(PKG_DIR, "README.md")
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(code_dir, "README_开发.md"))

    # 一并带上 Linux 传统部署套件（deploy/linux），使导出的包在目标机解压即包含
    # systemd / nginx / 运维脚本 / 部署文档，无需再从开发机单独传。
    deploy_src = os.path.join(PROJECT_DIR, "deploy", "linux")
    if os.path.isdir(deploy_src):
        shutil.copytree(deploy_src, os.path.join(code_dir, "deploy", "linux"),
                        ignore=ignore)


def zip_dir(src_dir, out_zip):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, src_dir)
                z.write(full, arc)


def main():
    ap = argparse.ArgumentParser(description="打包整套系统用于迁移")
    ap.add_argument("--with-data", action="store_true",
                    help="连行情/财务/指标大表一起导出（约 1.7GB）")
    ap.add_argument("--out", default=os.path.join(PROJECT_DIR, "dist"),
                    help="输出目录，默认 D:\\Stocks\\选股系统\\dist")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    root = os.path.join(args.out, f"迁移包_{stamp}")
    if os.path.exists(root):
        shutil.rmtree(root)
    code_dir, db_dir = os.path.join(root, "code"), os.path.join(root, "db")
    os.makedirs(code_dir)
    os.makedirs(db_dir)

    print("[1/4] 复制代码 ...")
    copy_code(code_dir)
    write_scripts(code_dir)
    pkgs = write_requirements(code_dir)

    print("[2/4] 导出数据库 ...")
    dump_exe = find_mysqldump()
    if not dump_exe:
        print("  [警告] 找不到 mysqldump，跳过数据库导出。")
        print("         可设环境变量 STOCK_MYSQL_BIN 指向 mysql 的 bin 目录后重跑。")
        files = []
    else:
        files = dump_sql(db_dir, dump_exe, args.with_data)
    db_mb = sum(os.path.getsize(p) for p in files) / 1024 / 1024
    for p in files:
        print(f"  {os.path.basename(p):<24} {os.path.getsize(p)/1024/1024:8.1f} MB")

    print("[3/4] 生成部署说明 ...")
    write_guide(root, args.with_data, db_mb)

    print("[4/4] 压缩 ...")
    out_zip = root + ".zip"
    zip_dir(root, out_zip)
    size_mb = os.path.getsize(out_zip) / 1024 / 1024

    print()
    print(f"完成：{out_zip}")
    print(f"体积：{size_mb:.1f} MB（含 SQL {db_mb:.1f} MB）")
    print(f"依赖：{', '.join(p.split('==')[0] for p in pkgs)}")
    print()
    print("目标机器上解压后，按「部署说明.md」操作。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
