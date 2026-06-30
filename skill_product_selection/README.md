# skill_product_selection · 1688 自动化选品助手

把"想个关键词 → 在 1688 一条条翻 → 比价比销量挑商家 → 手工整 Excel"这套体力活自动化。
选品人员用一句自然语言描述想选的产品，本 skill 跑通 **关键词 → 采集 → 分析 → 报告** 全流程，
最终交付一份 **Excel 数据表** + 一份 **Markdown 分析报告**。

报告的核心价值在 **"商家匹配度"** 一节——告诉选品人员每家商家到底专不专做这个品类、靠不靠谱，
这正是肉眼翻页最难快速判断、最耗时的部分。

> 这是一个 Claude / Agent **Skill**。真正驱动流程的是 [`SKILL.md`](SKILL.md) 里的指令，
> Agent 读它来编排下面的脚本。本 README 面向想了解项目结构、手动运行或二次开发的人。

---

## 适用场景

只要用户提到下面这类需求，就该用本 skill（哪怕没明说"用 1688 爬虫"）：

- 选品、找货源、找供应商
- 1688 / 阿里巴巴 比价
- 采集某类产品在 1688 的行情
- 想看某个品类的爆款 / 价格分布 / 优质商家

示例需求：`袜子`、`查一下工装袜`、`销量高的纯棉船袜`、`找一批便宜的纯棉船袜货源`。

---

## 核心前提：真实 Chrome 会话（无法自动化的一步）

1688 搜索页有高难度行为滑块，**唯一稳过风控的方式是复用一个已登录、已手动过滑块的真实 Chrome 标签页**
（CDP 调试端口 `9222`）。

- **登录** 和 **过滑块** 必须由人工完成，脚本只负责检测与引导。
- 启动调试版 Chrome（专用 profile，不影响日常 Chrome）由脚本/Agent 代做，用户只需在弹出的窗口里登录并手动搜索一次过滑块。
- `run_scraper.py` 抓取前会自动自检；未就绪时打印中文指引。

---

## 环境与依赖

- **Python 3.10 ~ 3.12**（`scrapling 0.4.9` 要求 ≥ 3.10）。
- 所有脚本统一使用 skill 自带的 **venv**，由 `bootstrap.py` 创建并返回其 python 绝对路径。
  - 采集和分析共用这同一个 venv。
  - 不要用系统 python，也不要假设装了 conda。
- 主要依赖：`scrapling`、`playwright`、`openpyxl`、`pandas`（及其传递依赖 lxml / curl_cffi 等）。
- 需本机安装 **Google Chrome**（用于调试会话）。

> 约定：下文 `$PY` = venv 的 python 绝对路径，`$SKILL` = 本 skill 目录。

---

## 快速开始（手动运行）

```bash
# 0. 准备环境（首次较慢，会下载/编译依赖；幂等，之后秒过）
PY=$(python3 "$SKILL/scripts/bootstrap.py" | tail -1)

# 1. 检查 Chrome 是否就绪（退出码 0=就绪，1=端口不通，2=端口通但未登录/无 1688 标签页）
$PY "$SKILL/scripts/setup_check.py"

# 1b. 若端口不通，后台启动调试版 Chrome，然后在弹出窗口里登录 1688 + 手动过一次滑块
bash "$SKILL/vendor/start_chrome_debug.sh" >/tmp/chrome_1688_debug.log 2>&1 &

# 2. 确定 workspace（建议在当前目录下按时间戳建子目录），逐个关键词采集
WS="$(pwd)/选品_纯棉船袜_$(date +%Y%m%d_%H%M)"
mkdir -p "$WS"
$PY "$SKILL/scripts/run_scraper.py" --keyword "纯棉船袜" --workspace "$WS"
$PY "$SKILL/scripts/run_scraper.py" --keyword "棉袜"     --workspace "$WS" --skip-check

# 3. 合并分析 → analysis.json + Excel
TS=$(date +%Y%m%d_%H%M)
$PY "$SKILL/scripts/analyze.py" \
  --inputs "$WS/1688_纯棉船袜.json" "$WS/1688_棉袜.json" \
  --workspace "$WS" --demand "销量高的纯棉船袜" --timestamp "$TS"

# 4. 读 $WS/analysis.json，按 references/report_template.md 写成 选品报告_$TS.md
```

---

## 脚本 CLI 速查

| 脚本 | 作用 | 关键参数 / 退出码 |
| --- | --- | --- |
| `scripts/bootstrap.py` | 建 venv、装依赖（幂等），最后一行输出 venv python 路径 | `--print` 只打印路径不安装；找不到 3.10+ 退 2 |
| `scripts/setup_check.py` | Chrome 9222 / 1688 标签页就绪自检 | `--json` 机器可读；退出码 `0` 就绪 / `1` 端口不通 / `2` 未登录 |
| `scripts/run_scraper.py` | 单关键词采集，产物落到 workspace | `--keyword`(必填) `--workspace`(必填) `--limit N`(0=第一屏全部) `--sleep` `--skip-check` |
| `scripts/analyze.py` | 合并去重 + 5 维度分析 → analysis.json + Excel | `--inputs`(一或多个 json，必填) `--workspace`(必填) `--demand` `--timestamp` |

> 采集只抓搜索列表页的 **第一屏**（约几十个，不翻页）。想覆盖更多就多跑几个相关关键词。
> 多关键词采集时，仅第一个词做就绪自检，后续词加 `--skip-check`。

---

## 分析与产物

`analyze.py` 合并多关键词结果、清洗去重后产出：

- **`analysis.json`** —— 含 5 个分析维度：价格分析、销量、供应商质量、产品属性热点，以及
  **`suppliers`** 客观商家信号列表（每家的主营品类、类目分布、商品名样本、命中商品、客观商家评分 0~1）。
  > `suppliers` 是给 Agent 做 **语义匹配判断** 的原料——脚本故意不下匹配结论（关键词匹配易误判，
  > 如需求"袜子"而类目叫"船袜/运动袜"）。
- **`选品数据_<ts>.xlsx`** —— 多工作表：合并明细 / 价格分析 / 销量Top / 优质供应商 / 商家信号 / 属性热点。

最终 **`选品报告_<ts>.md`** 由 Agent 按 [`references/report_template.md`](references/report_template.md) 撰写，结构：

1. 执行摘要
2. 价格分布
3. 款式与需求信号
4. **商家匹配度推荐 ★重点**（Agent 结合需求做语义判断：品类匹配高/中/低 × 客观商家评分）
5. 供应商质量概览
6. 产品属性热点
7. 选品建议

**报告价值定位**：本 skill 不是帮用户"从供应商销量里挑爆品照抄"，而是帮其 **洞察供给**——
品类在 1688 的价格分布、款式/卖点、商家质量、以及商家与需求的匹配度。销量只当作市场需求方向 / 款式验证的参考信号。

---

## 目录结构

```
skill_product_selection/
├── SKILL.md                      # Skill 指令（Agent 据此编排流程）
├── README.md                     # 本文件
├── scripts/
│   ├── bootstrap.py              # 建 venv、装依赖，返回 venv python 路径
│   ├── setup_check.py            # Chrome 9222 / 1688 就绪自检
│   ├── run_scraper.py            # 单关键词采集
│   └── analyze.py                # 合并去重 + 5 维度分析 + Excel 导出
├── vendor/                       # 已实测的 1688 爬虫，勿改
│   ├── scrape_1688.py
│   ├── extract.py
│   └── start_chrome_debug.sh     # 启动专用 profile 的调试版 Chrome
└── references/
    └── report_template.md        # Markdown 报告结构模板
```

> `vendor/` 是已实测的 1688 爬虫，**请勿修改**。

---

## 常见问题

- **依赖装不上 / 找不到 Python**：`bootstrap.py` 需要 3.10~3.12，可 `brew install python@3.12` 后重试。
- **`setup_check.py` 退出码非 0**：按上面"核心前提"启动调试 Chrome 并人工登录过滑块，再跑一次直到退出码为 0。
- **某关键词中途被风控拦截**：脚本会把被拦商品标记 `[被拦截]` 并继续，分析阶段自动过滤；整词失败时在 Chrome 手动重搜过滑块后可单独重跑该词。
- **采集时不要操作那个 Chrome 标签页**，否则可能打断抓取。
- **workspace 放哪**：默认在当前目录下按时间戳建子目录；基准目录不可写时应停下来问用户，**不要退回主目录 `~`**。
