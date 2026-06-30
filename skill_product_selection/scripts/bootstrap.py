"""自举脚本：检测 python3 → 在 skill 目录建 .venv → 装齐全部依赖（幂等）。

为什么需要它：用户机器上不能假设有 conda 或现成环境。这个脚本让 skill 自带
一套隔离的 Python 环境，采集(scrapling/playwright/openpyxl)和分析(pandas)共用一个 venv。

设计要点：
- 幂等：先用 venv 的 python 探测 4 个包是否齐全，齐全就直接打印路径退出，第二次运行秒过。
- 不跑 `playwright install chromium`：本方案通过 CDP 连用户真实 Chrome，playwright 只当
  CDP 客户端用，不需要它自带的浏览器内核 —— 省掉几百 MB 下载。
- 标准输出最后一行固定打印 venv python 的绝对路径，供调用方(skill / run_scraper / analyze)捕获。

用法：
  python3 scripts/bootstrap.py            # 建好环境
  python3 scripts/bootstrap.py --print    # 只打印 venv python 路径(若已就绪)，不安装
"""
import os
import sys
import shutil
import subprocess
import argparse

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(SKILL_DIR, ".venv")

# 依赖锁定版本对齐 mycode/requirements.txt（已实测可用的组合）。
# pandas 不锁版本，分析用到的 API 很稳定。
REQUIREMENTS = [
    "scrapling[fetchers]==0.4.9",
    "playwright==1.60.0",
    "openpyxl==3.1.5",
    "pandas",
]

# 探测用的 import 名（与包名不完全一致）。
PROBE_IMPORTS = ["scrapling", "playwright", "openpyxl", "pandas"]


def venv_python():
    """返回 venv 里 python 的绝对路径（不保证已存在）。"""
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def deps_ready(py):
    """用给定 python 探测 4 个包是否都能 import。"""
    if not os.path.exists(py):
        return False
    code = "import " + ", ".join(PROBE_IMPORTS)
    r = subprocess.run([py, "-c", code], capture_output=True)
    return r.returncode == 0


def py_version(exe):
    """返回 (major, minor) 或 None。"""
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            major, minor = out.stdout.strip().split(".")
            return int(major), int(minor)
    except Exception:
        pass
    return None


def find_base_python():
    """找一个 3.10~3.12 的解释器来建 venv。scrapling 0.4.9 要求 >=3.10，
    且 3.13+ 上某些依赖(lxml/curl_cffi)的 wheel 可能还没齐，故优先 3.10~3.12。

    搜索顺序：常见命名 → 常见安装路径 → 当前解释器兜底。找到第一个落在 [3.10,3.12] 的即用。"""
    candidates = ["python3.12", "python3.11", "python3.10", "python3", "python"]
    # 一些常见绝对路径（conda base / homebrew），尽量提高换机命中率。
    candidates += [
        os.path.expanduser("~/miniconda3/bin/python3.12"),
        os.path.expanduser("~/miniconda3/bin/python3.11"),
        os.path.expanduser("~/anaconda3/bin/python3.12"),
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
    ]

    checked = []
    for name in candidates:
        exe = shutil.which(name) if os.path.sep not in name else (name if os.path.exists(name) else None)
        if not exe:
            continue
        ver = py_version(exe)
        if ver is None:
            continue
        checked.append(f"{exe} ({ver[0]}.{ver[1]})")
        if (3, 10) <= ver <= (3, 12):
            return exe, ver

    # 没找到合适的：报清楚，列出查到的版本，给安装建议。
    print("!! 没有找到 3.10~3.12 的 Python（scrapling 0.4.9 需要 >=3.10）。", file=sys.stderr)
    if checked:
        print("   已检测到的解释器：" + "; ".join(checked), file=sys.stderr)
    print("   请安装 Python 3.10/3.11/3.12（如 `brew install python@3.12`，"
          "或安装 miniconda 后用其 python3.12），然后重试。", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只在依赖已就绪时打印 venv python 路径；未就绪则非零退出，不安装。")
    args = ap.parse_args()

    py = venv_python()

    # 幂等快路径：已就绪直接返回。
    if deps_ready(py):
        print(py)
        return

    if args.print_only:
        print("!! 依赖未就绪，请先运行: python3 scripts/bootstrap.py", file=sys.stderr)
        sys.exit(1)

    # 若已有 venv 但版本不达标（如之前用 3.9 建过），删掉重建。
    if os.path.exists(py):
        ev = py_version(py)
        if ev is None or not ((3, 10) <= ev <= (3, 12)):
            print(f"[bootstrap] 已有 venv 的 Python 版本不达标，重建: {VENV_DIR}", file=sys.stderr)
            shutil.rmtree(VENV_DIR, ignore_errors=True)

    # 建 venv（若不存在）。先找一个 3.10~3.12 的解释器作 base。
    if not os.path.exists(py):
        base, ver = find_base_python()
        print(f"[bootstrap] 使用 {base} (Python {ver[0]}.{ver[1]}) 创建虚拟环境: {VENV_DIR}",
              file=sys.stderr)
        subprocess.run([base, "-m", "venv", VENV_DIR], check=True)

    # 升级 pip，避免老 pip 装不动某些 wheel。
    print("[bootstrap] 升级 pip ...", file=sys.stderr)
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], check=True)

    print("[bootstrap] 正在安装依赖（首次较慢，scrapling/lxml/curl_cffi 等需下载或编译）...",
          file=sys.stderr)
    subprocess.run([py, "-m", "pip", "install", *REQUIREMENTS], check=True)

    if not deps_ready(py):
        print("!! 依赖安装后仍无法全部 import，请检查上面的 pip 输出。", file=sys.stderr)
        sys.exit(3)

    print("[bootstrap] 完成。", file=sys.stderr)
    # 最后一行固定为 venv python 绝对路径，供调用方捕获。
    print(py)


if __name__ == "__main__":
    main()
