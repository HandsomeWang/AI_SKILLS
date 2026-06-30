"""Chrome 9222 / 1688 标签页就绪自检。

爬虫唯一稳过 1688 搜索页风控的方式：复用一个【已登录、已手动过滑块】的真实 Chrome
标签页（CDP 端口 9222）。这个前置无法自动化（登录+过滑块要人工），所以跑爬虫前先自检，
未就绪就给出清晰的中文指引，让 skill / 用户照着做。

退出码：
  0 = 就绪（9222 通 且 有 1688 标签页）
  1 = 9222 不通（Chrome 没起或没开调试端口）
  2 = 9222 通但没有 1688 标签页（Chrome 起了但没打开/登录 1688）

用法：
  python3 scripts/setup_check.py
  python3 scripts/setup_check.py --json   # 机器可读输出
"""
import sys
import json
import argparse
import urllib.request

CDP_LIST_URL = "http://localhost:9222/json"

# 启动调试 Chrome 的脚本随 vendor 一起分发。
import os
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_SH = os.path.join(SKILL_DIR, "vendor", "start_chrome_debug.sh")

# 端口不通(code 1): 调试 Chrome 还没起 —— 这步【由 skill 替用户启动】, 不用用户敲命令。
GUIDE_NO_PORT = f"""\
调试版 Chrome 还没启动。请 skill 自己在后台启动它（不要让用户敲命令）：

  bash "{START_SH}" >/tmp/chrome_1688_debug.log 2>&1 &

它用一个专用 profile（不影响用户日常 Chrome）启动带调试端口 9222 的 Chrome，
并打开 1688 首页。启动后等几秒，再提示用户：
  「已打开 1688 调试浏览器，请在新窗口里登录 1688，并手动搜索一次、把滑块拖过去，弄好回复我。」
登录和过滑块必须用户亲手做（无法自动化）；登录态/滑块信任都绑在这个标签页上。"""

# 端口通但无 1688 标签页(code 2): Chrome 已起, 只差人工登录+过滑块。
GUIDE_NEED_LOGIN = """\
调试 Chrome 已就绪, 但还没有已登录的 1688 标签页。请提示用户：
  「请在调试浏览器里登录 1688，并手动搜索一次、把滑块拖过去，保持窗口开着，弄好回复我。」
登录和过滑块必须用户亲手做（无法自动化）。"""


def fetch_tabs():
    """返回 9222 上的标签页列表；连不上抛异常。"""
    with urllib.request.urlopen(CDP_LIST_URL, timeout=5) as resp:
        return json.load(resp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    result = {"ready": False, "code": 1, "reason": "", "tabs_1688": []}

    try:
        tabs = fetch_tabs()
    except Exception:
        result["reason"] = "无法连接 Chrome 调试端口 9222（Chrome 没启动或没开调试端口）。"
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("[未就绪] " + result["reason"])
            print()
            print(GUIDE_NO_PORT)
        sys.exit(1)

    tabs_1688 = [t.get("url", "") for t in tabs if "1688.com" in t.get("url", "")]
    result["tabs_1688"] = tabs_1688

    if not tabs_1688:
        result["code"] = 2
        result["reason"] = "Chrome 调试端口已通，但没有打开 1688 的标签页（请登录并搜索一次过滑块）。"
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("[未就绪] " + result["reason"])
            print()
            print(GUIDE_NEED_LOGIN)
        sys.exit(2)

    result["ready"] = True
    result["code"] = 0
    result["reason"] = f"就绪：检测到 {len(tabs_1688)} 个 1688 标签页。"
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("[就绪] " + result["reason"])
    sys.exit(0)


if __name__ == "__main__":
    main()
