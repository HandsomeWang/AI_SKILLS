"""1688 关键词搜索 -> 商品详情批量抓取 -> 提取商品/店铺信息 -> 导出 Excel。

架构 (经多轮实测收敛):
  - 抓取层: 复用你【已登录、已手动过滑块】的真实 Chrome 标签页 (CDP, 端口9222)。
            这是唯一能稳过 1688 搜索页风控的方式 —— 登录态+滑块信任都绑在该标签页上。
            (实测: Scrapling 的 cdp_url 会新开 context 丢信任; user_data_dir + headless 会撞搜索页风控。)
  - 店铺信息: 在标签页上手动挂 page.on("response"), 抓 mtop...shopcard 接口的 JSON
            (店铺名/url/回头率/服务分/发货率/好评率/入驻年/主营/标签, 一字段一key, 抗改版)。
  - 商品信息: Scrapling Selector 解析 HTML (标题/价格/属性/详情, JSON 接口里没有这些)。
  - 解析与抓取解耦: extract.extract_detail(html, shop_json=...)。

前置:
  1. bash mycode/start_chrome_debug.sh    # 启动带调试端口的真实 Chrome
  2. 在该 Chrome 登录 1688, 手动搜索一次过掉滑块, 保持窗口开着 (不要关!)
  3. 跑本脚本 (运行期间勿手动操作那个标签页)

用法:
  python mycode/scrape_1688.py "保温杯"
  python mycode/scrape_1688.py "保温杯" --limit 10
"""
import os
import re
import sys
import json
import time
import argparse
import urllib.request
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import extract_detail, extract_offerlist, extract_shop_established

CDP_VERSION_URL = "http://localhost:9222/json/version"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

COLUMNS = [
    "offer_id", "商品标题", "价格", "已售数量", "商品属性", "商品详情", "detail_url",
    "店铺名称", "店铺url", "店铺标签", "入驻年限", "主营品类",
    "店铺回头率", "店铺服务分", "采购咨询", "物流时效", "纠纷解决", "品质体验", "退换体验",
    "准时发货率", "店铺好评率", "所在地区", "成立时间",
    "店铺总商品数", "店铺商品分类", "店铺商品列表",
]


def get_ws_url():
    try:
        return json.load(urllib.request.urlopen(CDP_VERSION_URL, timeout=5))["webSocketDebuggerUrl"]
    except Exception:
        print("!! 无法连接 CDP (9222)。请先 bash mycode/start_chrome_debug.sh 启动 Chrome 并登录1688过滑块。")
        raise


def reuse_1688_page(ctx):
    """复用已有的 1688 标签页 (优先搜索页), 不新建 -> 保住登录态/滑块信任。"""
    for pg in ctx.pages:
        if "s.1688.com/selloffer" in pg.url:
            return pg
    for pg in ctx.pages:
        if "1688.com" in pg.url:
            return pg
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def is_blocked(html, url):
    return "punish" in url or "captcha" in url.lower() or "login.taobao.com" in url \
        or "Captcha Interception" in html


def parse_offer_ids(list_html):
    ids = re.findall(r'offerId["\':= ]+(\d{6,})', list_html)
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def hover_shop_card(page, settle=2500):
    """hover 店铺名, 触发悬浮卡片渲染 (所在地区/成立时间/企业面积等只在卡片里, 纯前端渲染)。
    卡片渲染进 DOM 后, page.content() 就能拿到这些字段。失败静默 (有的页面无卡片)。"""
    try:
        el = page.query_selector('.shop-company-name')
        if not el:
            return
        # 卡片在页面上部, 先滚回顶部附近再 hover, 否则元素在视口外 hover 不生效
        page.mouse.wheel(0, -6000)
        page.wait_for_timeout(400)
        el.hover()
        box = el.bounding_box()
        if box:  # 鼠标停在元素中心, 稳定触发 hover
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.wait_for_timeout(settle)
    except Exception:
        pass


def load_page(page, url, captured, wait_selector=None, wait_text=None,
              wait_timeout=8000, scrolls=5, scroll_pause=400, settle=800, hover_shop=False):
    """导航到 url, 等就绪信号, 滚动触发懒加载。captured 由 page.on(response) 持续填充。
    返回 (html, final_url)。每次导航前清空 captured, 使其只含本次导航的 XHR。

    就绪信号优先级 (命中即停, 不傻等):
      - wait_text: 轮询页面 HTML 直到出现该子串 (搜索页用, offerId 初始就在 HTML 里, 通常瞬间命中)。
      - wait_selector: 等该 CSS 选择器出现 (详情页用)。选错也只等 wait_timeout, 不再死等。
    hover_shop=True: 滚动后 hover 店铺名, 触发悬浮卡片 (取所在地区/成立时间), 详情页用。"""
    captured.clear()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    if wait_text:
        # 轮询 HTML 直到出现目标文本 (或超时)
        waited = 0
        while wait_text not in page.content() and waited < wait_timeout:
            page.wait_for_timeout(200)
            waited += 200
    elif wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=wait_timeout)
        except Exception:
            pass  # 超时也继续, 靠下面滚动+settle 兜底

    for _ in range(scrolls):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(scroll_pause)

    # 商品属性/详情在页面下部懒加载, 仅靠固定滚动+settle 可能抓在渲染之前(实测 offer
    # 921634471799 命中过: 标题/价格/店铺都拿到, 唯独属性+详情双空)。滚完显式等属性区块
    # 出现, 把它当就绪信号; 超时也不阻塞, 继续靠下面 settle 兜底。
    try:
        page.wait_for_selector('.normal-attributes-table tr, .decision-attributes-list li',
                               timeout=4000)
    except Exception:
        pass
    page.wait_for_timeout(settle)

    if hover_shop:
        hover_shop_card(page)
    return page.content(), page.url


def load_search_page(page, url, captured, max_scrolls=12, stable_rounds=2):
    """搜索页专用: 导航后自适应滚动 —— 直到 offerId 数量连续 stable_rounds 轮不再增长就停。
    比固定滚 N 次更聪明: 商品加载完立刻停 (不浪费), 商品多时也能滚到底 (不漏)。"""
    captured.clear()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # 等首批 offerId 出现 (通常瞬间)
    waited = 0
    while "offerId" not in page.content() and waited < 8000:
        page.wait_for_timeout(200)
        waited += 200

    prev, stable = -1, 0
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(400)
        cur = len(parse_offer_ids(page.content()))
        if cur <= prev:
            stable += 1
            if stable >= stable_rounds:
                break
        else:
            stable = 0
        prev = cur
    return page.content(), page.url


def load_offerlist(page, shop_url, captured):
    """进入商家商品列表页 (<shop_url>/page/offerlist.htm), 滚动一屏后返回 HTML。
    shop_url 形如 https://onenok.1688.com/ 或 https://onenok.1688.com。
    失败返回 None。"""
    if not shop_url:
        return None
    base = shop_url.rstrip('/')
    offerlist_url = base + '/page/offerlist.htm'
    try:
        captured.clear()
        page.goto(offerlist_url, wait_until="domcontentloaded", timeout=30000)
        # 等待商品或总数文字出现
        waited = 0
        while waited < 6000:
            if '件相关产品' in page.content() or 'offer-item' in page.content():
                break
            page.wait_for_timeout(300)
            waited += 300
        # 滚动一屏触发懒加载
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(400)
        page.wait_for_timeout(800)
        return page.content()
    except Exception as e:
        print(f"    [offerlist] 访问失败: {e}")
        return None


def shopcard_from_captured(captured):
    """从本商品捕获的 shopcard 响应里取含 shopName 的那条, 解析成 JSON。"""
    best = None
    for body in captured:
        if "shopName" in body and (best is None or len(body) > len(best)):
            best = body
    if best:
        try:
            return json.loads(best)
        except Exception:
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--limit", type=int, default=0, help="最多抓多少个商品, 0=全部")
    ap.add_argument("--sleep", type=float, default=1.5, help="商品之间停顿秒数")
    args = ap.parse_args()

    kw = args.keyword
    search_url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(kw.encode('gbk'))}"

    safe_kw = re.sub(r'[\\/:*?"<>|]', '_', kw)
    xlsx_path = f"{OUT_DIR}/1688_{safe_kw}.xlsx"
    json_path = f"{OUT_DIR}/1688_{safe_kw}.json"

    # 边抓边写: 先建好表头, 每抓一条 append + save 一次, 中途被风控打断也不丢已抓数据。
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "商品+店铺"
    ws.append(COLUMNS)
    for col_idx, name in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = \
            40 if name in ("商品标题", "商品属性", "商品详情") else 16
    wb.save(xlsx_path)

    rows = []

    def flush(rec):
        """把一条记录立即追加到 Excel 并落盘, 同时刷新 JSON 备份。"""
        rows.append(rec)
        ws.append([rec.get(c, "") for c in COLUMNS])
        wb.save(xlsx_path)
        json.dump(rows, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    ws_url = get_ws_url()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        ctx = browser.contexts[0]
        page = reuse_1688_page(ctx)
        print(f"[*] 复用标签页, 当前: {page.url[:60]}")

        # 挂 response 监听 (抓 shopcard), captured 每次导航前清空
        captured = []
        def on_resp(resp):
            if "shopcard" in resp.url:
                try:
                    body = resp.text()
                    if "shopName" in body:
                        captured.append(body)
                except Exception:
                    pass
        page.on("response", on_resp)

        # 1) 搜索列表页 (复用标签页, 信任已就绪)
        print(f"[*] 打开搜索页: {kw}")
        # 搜索页: offerId 分批懒加载, 滚动直到数量不再增长 (加载完即停, 不死等)。
        list_html, list_final = load_search_page(page, search_url, captured)
        with open(f"{OUT_DIR}/list_page.html", "w", encoding="utf-8") as f:
            f.write(list_html)
        if is_blocked(list_html, list_final):
            print("!! 搜索页被拦截。请在该 Chrome 手动搜一次过滑块后重跑。")
            sys.exit(1)

        offer_ids = parse_offer_ids(list_html)
        print(f"[*] 列表页解析到 {len(offer_ids)} 个商品 offerId")
        if args.limit > 0:
            offer_ids = offer_ids[:args.limit]
            print(f"[*] 按 --limit 仅抓前 {len(offer_ids)} 个")

        # 2) 逐个详情页
        for idx, oid in enumerate(offer_ids, 1):
            durl = f"https://detail.1688.com/offer/{oid}.html"
            print(f"  [{idx}/{len(offer_ids)}] {oid} ...", end=" ", flush=True)
            try:
                dhtml, dfinal = load_page(page, durl, captured,
                                          wait_selector=".module-od-title .title-content",
                                          hover_shop=True)
                if is_blocked(dhtml, dfinal):
                    print("被拦截, 跳过 (请手动过验证后重跑剩余)")
                    flush({"offer_id": oid, "detail_url": durl, "商品标题": "[被拦截]"})
                    continue
                shop_json = shopcard_from_captured(captured)
                rec = extract_detail(dhtml, offer_id=oid, detail_url=durl, shop_json=shop_json)

                # 进入商家商品列表页, 抓取总数/分类/商品列表, 顺便补全成立时间
                shop_url = rec.get("店铺url", "")
                offerlist_html = load_offerlist(page, shop_url, captured)
                if offerlist_html:
                    ol = extract_offerlist(offerlist_html)
                    rec["店铺总商品数"] = ol["total_count"] if ol["total_count"] is not None else ""
                    rec["店铺商品分类"] = json.dumps(ol["categories"], ensure_ascii=False) if ol["categories"] else ""
                    rec["店铺商品列表"] = json.dumps(ol["products"], ensure_ascii=False) if ol["products"] else ""
                    # 若商品详情页没拿到成立时间, 从店铺页补全
                    if not rec.get("成立时间"):
                        rec["成立时间"] = extract_shop_established(offerlist_html)
                    # 主营品类兜底: shopcard JSON 缺 mainCategoryName 且详情页也没解析到时
                    # (实测服装类部分店铺会这样)。店铺页 (offerlist.htm) 头部有
                    # 「主营类目: xxx」文字, 从这里补全。
                    if not rec.get("主营品类") and ol.get("main_category"):
                        rec["主营品类"] = ol["main_category"]
                else:
                    rec.setdefault("店铺总商品数", "")
                    rec.setdefault("店铺商品分类", "")
                    rec.setdefault("店铺商品列表", "")

                flush(rec)
                tag = "JSON" if shop_json else "HTML"
                print(f"OK[{tag}] {rec.get('商品标题','')[:16]} | {rec.get('价格','')} | 好评{rec.get('店铺好评率','')} | 店铺商品{rec.get('店铺总商品数','?')}件")
            except Exception as e:
                print(f"ERR {e}")
                flush({"offer_id": oid, "detail_url": durl, "商品标题": f"[错误]{e}"})
            time.sleep(args.sleep)

        browser.close()  # connect_over_cdp 下仅断开, 不关你的 Chrome

    # 数据已在循环里边抓边写, 这里只做最终汇总。
    ok = sum(1 for r in rows if r.get("商品标题") and not r["商品标题"].startswith("["))
    js = sum(1 for r in rows if r.get("店铺好评率"))
    print(f"\n[完成] 共 {len(rows)} 行, 成功 {ok} 行, 店铺数据命中 {js} 行")
    print(f"  Excel -> {xlsx_path}")
    print(f"  JSON  -> {json_path}")


if __name__ == "__main__":
    main()
