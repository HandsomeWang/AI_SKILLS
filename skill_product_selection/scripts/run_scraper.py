"""爬虫薄封装：对单个关键词跑 vendor/scrape_1688.py，把产物落到指定 workspace。

为什么要包一层：
- vendor/scrape_1688.py 是已实测验证的脚本，把输出写死在它自己所在目录。为了不改它、
  又能把每次选品的产物归集到独立 workspace，这里用 subprocess 调它，跑完再把
  1688_<关键词>.{xlsx,json} 移动到 workspace。
- 跑之前先做 setup_check（Chrome 9222 / 1688 标签页就绪），未就绪直接带提示退出，
  避免白跑。

用法（用 .venv 的 python 跑）：
  <venv-python> scripts/run_scraper.py --keyword "纯棉船袜" --workspace /path/to/ws --limit 0
"""
import os
import re
import sys
import shutil
import argparse
import subprocess

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(SKILL_DIR, "vendor")
SCRAPER = os.path.join(VENDOR, "scrape_1688.py")
SETUP_CHECK = os.path.join(SKILL_DIR, "scripts", "setup_check.py")


def safe_kw(kw):
    """与 scrape_1688.py 内部一致的文件名净化规则。"""
    return re.sub(r'[\\/:*?"<>|]', "_", kw)


def run_setup_check():
    """跑就绪自检；未就绪时把它的指引透传出来并退出。"""
    r = subprocess.run([sys.executable, SETUP_CHECK])
    if r.returncode != 0:
        # setup_check 已经把中文指引打到 stdout 了，这里直接带同样的退出码退出。
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--workspace", required=True, help="本次选品的产物目录")
    ap.add_argument("--limit", type=int, default=0, help="最多抓多少个商品, 0=该搜索页第一屏全部")
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--skip-check", action="store_true", help="跳过 Chrome 就绪自检（多关键词时只查一次）")
    args = ap.parse_args()

    os.makedirs(args.workspace, exist_ok=True)

    if not args.skip_check:
        run_setup_check()

    # 跑爬虫（用当前解释器，应当是 .venv 的 python）。
    cmd = [sys.executable, SCRAPER, args.keyword, "--sleep", str(args.sleep)]
    if args.limit > 0:
        cmd += ["--limit", str(args.limit)]
    print(f"[run_scraper] 抓取关键词: {args.keyword}", file=sys.stderr)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"!! 爬虫退出码非零({r.returncode})，关键词: {args.keyword}", file=sys.stderr)
        sys.exit(r.returncode)

    # 把产物从 vendor/ 移到 workspace。
    skw = safe_kw(args.keyword)
    moved = []
    for ext in ("json", "xlsx"):
        src = os.path.join(VENDOR, f"1688_{skw}.{ext}")
        if os.path.exists(src):
            dst = os.path.join(args.workspace, f"1688_{skw}.{ext}")
            shutil.move(src, dst)
            moved.append(dst)
    # list_page.html 是副产物，搬走以免污染 vendor。
    snap = os.path.join(VENDOR, "list_page.html")
    if os.path.exists(snap):
        shutil.move(snap, os.path.join(args.workspace, f"list_page_{skw}.html"))

    if not moved:
        print(f"!! 未找到产物文件 1688_{skw}.json/xlsx，可能被风控拦截或无结果。", file=sys.stderr)
        sys.exit(4)

    # 最后一行打印本次产生的 JSON 路径，供 analyze.py 收集。
    json_path = os.path.join(args.workspace, f"1688_{skw}.json")
    print(json_path)


if __name__ == "__main__":
    main()
