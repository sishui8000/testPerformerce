#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
编译吞吐基准：用同一份负载对比 GitHub Actions runner 与本机的 C 编译效率。

特点：
  * 自校准：先单线程试编 3 个编译单元(TU)，估算每 TU 耗时，再自动放大 TU 数量，
    使主测试刚好跑满 --target 秒（默认 300s，即 5 分钟）。
  * 固定并行度 --jobs 2，与 GitHub 那边保持一致，双方可比。
  * 输出每 TU 的墙钟耗时、CPU 时间和并行系数；并行系数低说明 IO/调度是瓶颈。

用法：
    python3 build_bench.py --jobs 2 --target 300
    python3 build_bench.py --jobs 2 --target 300 --cc gcc
    python3 build_bench.py --jobs 2 --target 300 --work /tmp/x --keep

注意：要跨机器对比，必须用同一编译器（gcc）——Windows 上请用 MSVC 编译的
数值只用于脚本自检，不要和 runner 的 gcc 结果直接比较。
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

NFUNCS = 600          # 头文件里的 inline 函数数量（模拟真实头文件解析成本）
CALLS_PER_TU = 200    # 每个编译单元调用多少个 inline 函数
CALIB_TUS = 6         # 校准时并行编译的 TU 数量（与正式测试同并行度）
IO_FILES = 1500       # 小文件读写测试的文件数
IO_SIZE = 4096


# ---------------------------------------------------------------- 环境信息

def sh(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return (p.stdout + p.stderr).strip()
    except Exception:
        return ""


def cpu_name():
    if os.path.exists("/proc/cpuinfo"):
        txt = open("/proc/cpuinfo", encoding="utf-8", errors="ignore").read()
        m = re.search(r"model name\s*:\s*(.+)", txt)
        if m:
            return m.group(1).strip()
        m = re.search(r"Hardware\s*:\s*(.+)", txt)
        if m:
            return m.group(1).strip()
    if platform.system() == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    return platform.processor() or platform.machine()


def mem_gb():
    if os.path.exists("/proc/meminfo"):
        for line in open("/proc/meminfo", encoding="utf-8", errors="ignore"):
            if line.startswith("MemTotal"):
                return int(line.split()[1]) / 1024 / 1024
    if platform.system() == "Windows":
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MemStatus()
        st.dwLength = ctypes.sizeof(MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / (1024 ** 3)
    return 0.0


def fs_type(path):
    out = sh(["findmnt", "-no", "FSTYPE", "-T", path])
    if out:
        return out.splitlines()[0]
    out = sh(["df", "-Th", path])
    lines = out.splitlines()
    if len(lines) >= 2:
        return lines[1].split()[1]
    return "unknown"


def cgroup_cpus():
    """容器的 CPU 配额，GitHub runner 上 nproc 与配额一致时返回 None。"""
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max":
            return round(int(quota) / int(period), 2)
    except Exception:
        pass
    return None


def detect_cc(prefer):
    if prefer and prefer != "auto":
        candidates = [prefer]
    else:
        candidates = ["gcc", "cc", "clang", "cl"]
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        if name == "cl":
            out = sh([exe])
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            return "cl", exe, "MSVC %s" % m.group(1) if m else "MSVC"
        out = sh([exe, "--version"])
        if out:
            return name, exe, out.splitlines()[0].strip()
    return None, None, None


# ---------------------------------------------------------------- 负载生成

def gen_header(path):
    parts = [
        "#ifndef BENCH_H",
        "#define BENCH_H",
        "#include <stdint.h>",
        "#include <stdio.h>",
        "#include <string.h>",
        "",
        "static inline uint32_t bench_mix(uint32_t x) {",
        "  x ^= x >> 16; x *= 0x7feb352dU; x ^= x >> 15; "
        "x *= 0x846ca68bU; x ^= x >> 16;",
        "  return x;",
        "}",
        "",
    ]
    for i in range(NFUNCS):
        k1 = (i * 2654435761 + 12345) & 0xFFFFFFFF
        k2 = (i * 40503 + 7919) & 0xFFFFFFFF
        s = (i % 13) + 1
        parts.append(
            "static inline uint32_t bench_f%d(uint32_t x) {\n"
            "  uint32_t a = x ^ 0x%08xU;\n"
            "  a = (a << %d) | (a >> %d);\n"
            "  a = bench_mix(a + 0x%08xU);\n"
            "  return a ^ 0x%08xU;\n"
            "}"
            % (i, k1, s, 32 - s, k2, k1 ^ k2))
    parts.append("#endif")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def gen_tu(path, idx):
    step = max(1, NFUNCS // CALLS_PER_TU)
    lines = [
        '#include "bench.h"',
        "",
        "volatile uint32_t bench_sink_%d;" % idx,
        "",
        "uint32_t tu_%04d_run(uint32_t seed) {" % idx,
        "  uint32_t a = seed;",
    ]
    for j in range(0, NFUNCS, step):
        lines.append("  a = bench_f%d(a + %dU);" % (j, j))
    lines += [
        "  bench_sink_%d = a;" % idx,
        "  return a;",
        "}",
        "",
        "int tu_%04d_fmt(uint32_t a, char *out) {" % idx,
        '  snprintf(out, 64, "%u", a);',
        "  return (int)strlen(out);",
        "}",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cc_cmd(kind, exe, src, obj):
    if kind == "cl":
        return [exe, "/nologo", "/O2", "/c", src, "/Fo" + obj]
    return [exe, "-O2", "-pipe", "-c", src, "-o", obj]


def compile_batch(kind, exe, workdir, srcs, jobs):
    """返回 (墙钟秒, 子进程CPU秒, 错误列表)。"""
    errors = []

    def one(src):
        obj = src[:-2] + ".o"
        p = subprocess.run(cc_cmd(kind, exe, src, obj), cwd=workdir,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, errors="replace")
        if p.returncode != 0:
            return "%s: %s" % (os.path.basename(src),
                               (p.stderr or p.stdout).strip()[:300])
        return None

    t0 = time.perf_counter()
    c0 = os.times()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for err in pool.map(one, srcs):
            if err:
                errors.append(err)
    wall = time.perf_counter() - t0
    c1 = os.times()
    cpu = (c1.user - c0.user) + (c1.system - c0.system)
    return wall, cpu, errors


def io_test(root):
    d = os.path.join(root, "io")
    os.makedirs(d, exist_ok=True)
    data = b"x" * IO_SIZE
    t0 = time.perf_counter()
    for i in range(IO_FILES):
        with open(os.path.join(d, "f%05d.bin" % i), "wb") as f:
            f.write(data)
    t1 = time.perf_counter()
    for i in range(IO_FILES):
        with open(os.path.join(d, "f%05d.bin" % i), "rb") as f:
            f.read()
    t2 = time.perf_counter()
    shutil.rmtree(d, ignore_errors=True)
    return t1 - t0, t2 - t1


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=2, help="并行编译任务数（默认 2）")
    ap.add_argument("--target", type=float, default=300.0,
                    help="主测试目标时长，秒（默认 300）")
    ap.add_argument("--cc", default="auto", help="编译器：auto/gcc/clang/cl")
    ap.add_argument("--work", default=".bench-work", help="工作目录")
    ap.add_argument("--out", default="bench-result.txt", help="结果文件")
    ap.add_argument("--keep", action="store_true", help="保留工作目录")
    ap.add_argument("--skip-io", action="store_true", help="跳过小文件读写测试")
    args = ap.parse_args()

    kind, exe, ccver = detect_cc(args.cc)
    if not kind:
        sys.exit("未找到可用的 C 编译器（gcc/clang/cl）。"
                 "Linux 上先安装 build-essential。")

    work = os.path.abspath(args.work)
    src_dir = os.path.join(work, "src")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(src_dir)

    nproc = os.cpu_count() or 1
    try:
        avail = len(os.sched_getaffinity(0))
    except AttributeError:
        avail = nproc
    quota = cgroup_cpus()
    fstype = fs_type(work)

    print("=" * 62)
    print("编译吞吐基准  Build Throughput Benchmark")
    print("=" * 62)
    print("时间          : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("平台          : %s %s" % (platform.system(), platform.release()))
    print("CPU           : %s" % cpu_name())
    print("逻辑核心      : %d（可用 %d）" % (nproc, avail))
    if quota:
        print("cgroup CPU 配额: %.2f 核" % quota)
    print("内存          : %.1f GB" % mem_gb())
    print("编译器        : %s" % ccver)
    print("工作目录      : %s (%s)" % (work, fstype))
    print("并行任务数    : %d" % args.jobs)
    print("目标时长      : %.0f s" % args.target)
    print("-" * 62)

    # 1) IO 测试
    io_wall = io_read = 0.0
    if not args.skip_io:
        print("IO 测试：写入并读回 %d 个 %d 字节小文件 ..." % (IO_FILES, IO_SIZE))
        io_wall, io_read = io_test(work)
        print(" 写入 %.3fs (%.0f 文件/s)  读取 %.3fs (%.0f 文件/s)"
              % (io_wall, IO_FILES / max(io_wall, 1e-6),
                 io_read, IO_FILES / max(io_read, 1e-6)))
        if fstype in ("9p", "cifs", "vboxsf", "fuse", "drvfs", "v9fs"):
            print(" [!] 工作目录位于共享/虚拟文件系统(%s)，编译会明显变慢" % fstype)

    # 2) 生成负载 + 校准
    gen_header(os.path.join(src_dir, "bench.h"))
    t_gen0 = time.perf_counter()
    cal = []
    for i in range(CALIB_TUS):
        p = os.path.join(src_dir, "cal_%02d.c" % i)
        gen_tu(p, 0)
        cal.append(p)
    t_gen = time.perf_counter() - t_gen0

    print("校准：并行编译 %d 个 TU（-j%d）..." % (CALIB_TUS, args.jobs))
    cal_wall, cal_cpu, errs = compile_batch(kind, exe, src_dir, cal, args.jobs)
    if errs:
        sys.exit("编译失败：\n" + "\n".join(errs))
    per_tu = cal_wall / CALIB_TUS
    total_tus = int(round(args.target / max(per_tu, 1e-4)))
    total_tus = max(args.jobs * 2, min(total_tus, 4000))
    print("  -j%d 下单 TU 平均 %.3fs → 主测试 %d 个 TU（预计 %.0fs）"
          % (args.jobs, per_tu, total_tus, total_tus * per_tu))

    # 3) 正式测试（校准有偏差时自动补测，保证总时长贴近 target）
    def make_srcs(start, count):
        out = []
        for i in range(start, start + count):
            p = os.path.join(src_dir, "tu_%04d.c" % i)
            gen_tu(p, i)
            out.append(p)
        return out

    t_gen0 = time.perf_counter()
    srcs = make_srcs(0, total_tus)
    gen_cost = time.perf_counter() - t_gen0

    print("主测试：%s -O2 -j%d 编译 %d 个 TU ...（约需 %.0f 秒，请勿中断）"
          % (kind, args.jobs, total_tus, args.target))
    wall, cpu, errs = compile_batch(kind, exe, src_dir, srcs, args.jobs)
    if errs:
        sys.exit("编译失败：\n" + "\n".join(errs[:5]))
    tus = total_tus

    remain = args.target - wall
    if remain > args.target * 0.15:
        extra = max(args.jobs, int(remain / max(wall / tus, 1e-4)))
        print("补测：再加 %d 个 TU（已跑 %.0fs / 目标 %.0fs）"
              % (extra, wall, args.target))
        srcs2 = make_srcs(tus, extra)
        w2, c2, errs = compile_batch(kind, exe, src_dir, srcs2, args.jobs)
        if errs:
            sys.exit("编译失败：\n" + "\n".join(errs[:5]))
        wall += w2
        cpu += c2
        tus += extra

    if platform.system() == "Windows":
        cpu = 0.0  # Windows 上拿不到子进程 CPU 时间，避免给出误导性数值

    per_tu_wall = wall / tus
    per_tu_cpu = cpu / tus if cpu else 0.0
    parallelism = cpu / wall if cpu else 0.0
    score = tus / wall

    cpu_line = ("%.2fs (%.2fs/TU, 并行系数 %.2f)"
                % (cpu, per_tu_cpu, parallelism)) if cpu else "n/a"
    print("-" * 62)
    print("编译单元总数  : %d" % tus)
    print("墙钟总耗时    : %.2f s" % wall)
    print("CPU 时间      : %s" % cpu_line)
    print("单 TU 墙钟    : %.4f s" % per_tu_wall)
    print("吞吐得分      : %.3f TU/s（越大越好）" % score)
    print("等效千 TU 耗时: %.1f s / 1000 TU" % (1000 * per_tu_wall))
    print("源码生成耗时  : %.2fs（校准 %.2fs，含在总时长之外）"
          % (gen_cost, t_gen))
    print("=" * 62)

    # 4) 结果落盘（方便 Actions artifact 收集）
    lines = [
        "BENCH RESULT",
        "time=%s" % time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host=%s %s" % (platform.system(), platform.release()),
        "cpu=%s" % cpu_name(),
        "cores=%d avail=%d cgroup_quota=%s" % (nproc, avail, quota),
        "mem_gb=%.1f" % mem_gb(),
        "compiler=%s" % ccver,
        "fs=%s" % fstype,
        "jobs=%d" % args.jobs,
        "tus=%d" % tus,
        "wall_s=%.2f" % wall,
        "cpu_s=%.2f" % cpu,
        "parallel=%.2f" % parallelism,
        "per_tu_s=%.4f" % per_tu_wall,
        "score_tu_per_s=%.3f" % score,
        "io_write_fps=%.0f" % (IO_FILES / io_wall if io_wall else 0),
        "io_read_fps=%.0f" % (IO_FILES / io_read if io_read else 0),
    ]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("结果已写入 %s" % args.out)

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
