#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 mysqldump 生成的「扩展 INSERT」（单条 VALUES 含多行）改写为每行一条 INSERT。

用途：迁移导入时若报 `ERROR 2013 Lost connection`（单条超长 INSERT 超过
max_allowed_packet），可用本脚本把现有的 03_fact_data.sql 就地改写为行级
INSERT，免去回源机器重新 mysqldump 再传几 GB 的麻烦。

用法：
    python3 split_fact_inserts.py 03_fact_data.sql 03_fact_data_split.sql

说明：
- 只改写以 INSERT 开头、且含 ` VALUES ` 的扩展 INSERT 行；其它行（USE / SET /
  CREATE 等）原样透传。
- 按「顶层逗号」（不在括号里的逗号）切分各个值元组，每个元组输出一条
  `INSERT ... VALUES (...);`。
- 逐行流式处理，内存占用与单条最大行一致，可直接处理数 GB 文件。
- 输出文件会比输入大约 2~3 倍（行级 INSERT 冗余更多），导入前请确认磁盘空间
  充足（先 `df -h`）。
"""
import sys


def split_line(line: str) -> str:
    stripped = line.lstrip()
    if not stripped[:6].upper() == "INSERT":
        return line

    idx = stripped.upper().find(" VALUES ")
    if idx == -1:
        return line

    head = stripped[:idx]              # INSERT INTO `t` (`col1`,`col2`,...)
    rest = stripped[idx + 7:]          # (...),(...),...;  或 (...),(...),...

    # 去掉结尾分号与换行
    if rest.endswith(";\n"):
        rest_body = rest[:-2]
    elif rest.endswith(";"):
        rest_body = rest[:-1]
    else:
        rest_body = rest.rstrip("\n")

    # 按顶层逗号切分各个值元组
    groups = []
    depth = 0
    cur = []
    for ch in rest_body:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            groups.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        groups.append(''.join(cur))

    out = []
    for g in groups:
        g = g.strip()
        if not g:
            continue
        out.append(f"{head} VALUES {g};\n")
    return ''.join(out)


def main():
    if len(sys.argv) != 3:
        print("usage: python3 split_fact_inserts.py <in.sql> <out.sql>")
        sys.exit(1)

    inp, outp = sys.argv[1], sys.argv[2]
    print(f"[*] 读取 {inp} ...")
    n_insert = 0
    with open(inp, 'r', encoding='utf-8', errors='replace', newline='') as f, \
         open(outp, 'w', encoding='utf-8', newline='\n') as o:
        for line in f:
            res = split_line(line)
            o.write(res)
            if res[:6].upper() == "INSERT":
                n_insert += 1
    print(f"[+] 完成，共写出 {n_insert} 条 INSERT -> {outp}")


if __name__ == '__main__':
    main()
