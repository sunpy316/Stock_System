# -*- coding: utf-8 -*-
"""A股选股系统 · 服务管理（仅标准库）

用法：
    python service.py status      查看 3306 / 8800 端口占用情况
    python service.py stop        停止这两个端口上的服务（需输入 y 确认）
    python service.py stop -y     同上，跳过确认（给 bat 调用）

设计要点：
1. 只按端口定位进程，不按进程名全杀，避免误伤同机其他服务。
2. 停之前核对可执行文件名（mysqld.exe / python.exe），名字不符就拒绝，
   防止端口被别的程序占用时误杀。
3. 用 ctypes 调 Win32 API，不依赖 taskkill / wmic 这类常被安全策略拦的工具。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

PORTS = {3306: "MySQL 数据库", 8800: "Web 服务"}
ALLOWED_EXE = {"mysqld.exe", "python.exe"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _listening_pids(port):
    """返回监听指定端口的 PID 集合。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            check=False,
        ).stdout
    except OSError:
        return set()
    text = out.decode("mbcs", errors="ignore")
    pids = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[4]
        if state != "LISTENING":
            continue
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        if pid.isdigit():
            pids.add(int(pid))
    return pids


def _exe_name(pid):
    """取进程可执行文件名；失败返回 None。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return None
    finally:
        kernel32.CloseHandle(handle)


def _terminate(pid):
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False, ctypes.get_last_error()
    try:
        ok = kernel32.TerminateProcess(handle, 1)
        return bool(ok), (0 if ok else ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def cmd_status():
    any_up = False
    for port, label in PORTS.items():
        pids = _listening_pids(port)
        if not pids:
            print(f"  [ stop ] {port}  {label}      未运行")
            continue
        any_up = True
        for pid in sorted(pids):
            print(f"  [  OK  ] {port}  {label}      PID {pid} ({_exe_name(pid) or '未知'})")
    return 0 if any_up else 1


def cmd_stop(assume_yes=False):
    targets = []
    for port, label in PORTS.items():
        for pid in sorted(_listening_pids(port)):
            exe = _exe_name(pid)
            targets.append((port, label, pid, exe))

    if not targets:
        print("  没有运行中的服务，无需停止。")
        return 0

    print("  将停止以下进程：")
    blocked = []
    for port, label, pid, exe in targets:
        mark = "" if (exe or "").lower() in ALLOWED_EXE else "  <- 进程名不在白名单，将跳过"
        print(f"    {port}  {label}  PID {pid} ({exe or '未知'}){mark}")
        if (exe or "").lower() not in ALLOWED_EXE:
            blocked.append(pid)
    print()

    if not assume_yes:
        ans = input("  确认停止？(y/N): ").strip().lower()
        if ans not in ("y", "yes", "是"):
            print("  已取消。")
            return 0

    failed = 0
    for port, label, pid, exe in targets:
        if pid in blocked:
            print(f"  跳过 PID {pid}：进程名不是 mysqld.exe / python.exe")
            failed += 1
            continue
        ok, err = _terminate(pid)
        if ok:
            print(f"  已停止 {label}  PID {pid}")
        else:
            print(f"  停止失败 {label}  PID {pid}  错误码 {err}")
            failed += 1

    print()
    print("  完成。" if not failed else f"  完成，但有 {failed} 项未处理。")
    return 0 if not failed else 2


def main():
    ap = argparse.ArgumentParser(description="A股选股系统服务管理")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="查看端口占用")
    p_stop = sub.add_parser("stop", help="停止服务")
    p_stop.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "stop":
        return cmd_stop(args.yes)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
