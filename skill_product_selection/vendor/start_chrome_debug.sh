#!/bin/bash
# 启动带远程调试端口的真实 Chrome，供 Scrapling 通过 CDP 连接抓取 1688。
# 用法：在普通终端执行  bash mycode/start_chrome_debug.sh
# 启动后请在该 Chrome 中登录 1688，并手动搜索一次过掉滑块，然后保持窗口开着。

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/chrome-1688-debug"

echo ">>> 使用专用 profile: $PROFILE"
echo ">>> 调试端口: 9222"
echo ">>> 启动后请在该浏览器登录 1688 并手动搜索过滑块，然后保持窗口开着。"

"$CHROME" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE" \
  "https://www.1688.com/"
