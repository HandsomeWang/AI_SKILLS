"""详情页字段提取 (被 scrape_1688.py import)。

  - 商品信息(标题/价格/属性/详情): 解析 HTML (这些字段 shopcard JSON 接口里没有)。
  - 店铺信息(店铺名/url/回头率/服务分/发货率/好评率/入驻年/主营/标签): 优先读 shopcard XHR JSON
    (mtop.1688.moga.pc.shopcard, 一字段一 key, 抗改版), HTML 仅作 JSON 缺失时的兜底。

输入: html 字符串 + 可选 shop_json(dict, 即 shopcard 接口的完整响应 JSON)。
输出: dict。
"""
import re
import json
from scrapling.parser import Selector


def _txt(el, strip=True):
    if el is None:
        return ""
    try:
        return el.get_all_text(strip=strip)
    except Exception:
        return ""


def _first(page, sel):
    els = page.css(sel)
    return els[0] if els else None


# 商品详情区里 1688 的固定平台模板段落, 过滤掉
_DESC_NOISE = [
    '【平台活动下价格】', '【非平台活动下价格】', '活动前价格', '划线价格',
    '非分销场景', '分销场景', '前述价格未计算', '采购津贴', '跨店券',
    '内容声明', '阿里巴巴中国站为第三方', '请立即向阿里巴巴举报',
    '数据的延迟性', '取价时间', '指商家自营销活动场景', '并非原价',
    '销售指导价', '该价格不包含平台', '商品的曾经展示', '提醒您购买商品',
    '谨慎核实', '阿里旺旺', '海量店铺', '真实性、准确性和合法性',
]
_DESC_TEMPLATE_MARKERS = ['发布价', '全网销量', '划线价格', '平台活动下价格',
                          '由商家自行设置的销售标价', '成交价格根据商家设置']

# shopcard 接口 iconType / shopType -> 店铺标签中文。
# 注: 源头旗舰的 iconType 实测是 'ytqj' (不是 ytqjd)。未知类型回退为原始值, 避免静默丢标签。
ICON_MAP = {
    'cjgc': '超级工厂', 'ytgc': '源头工厂',
    'ytqj': '源头旗舰', 'ytqjd': '源头旗舰店',
    'slsj': '实力商家', 'jpzz': '金牌制造', 'rzgc': '认证工厂',
}


def _clean_desc(desc):
    if not desc:
        return ""
    out = []
    for ln in (l.strip() for l in desc.split('\n')):
        if not ln or ln == '商品详情':
            continue
        if any(noise in ln for noise in _DESC_NOISE):
            continue
        out.append(ln)
    cleaned = "\n".join(out).strip()
    marker_hits = sum(1 for mk in _DESC_TEMPLATE_MARKERS if mk in cleaned)
    if marker_hits >= 2 and len(cleaned) < 400:
        return ""
    return cleaned


def _shop_from_json(d, shop_json):
    """从 shopcard 接口 JSON 填充店铺字段。返回 True 表示成功填了主字段。"""
    try:
        model = shop_json.get("data", {}).get("model")
    except AttributeError:
        return False
    if not model:
        return False

    d["店铺名称"] = model.get("shopName", "")
    d["店铺url"] = model.get("shopUrl", "")
    # 入驻年限: shopcard 的 tpYear 只有数字无单位, 先拼"年"作兜底;
    # 后面会用页面 .shop-tp-year 的真实文本(含单位)覆盖, 防止单位实际是"月"时出错。
    ty = model.get("tpYear")
    d["入驻年限"] = f"{ty}年" if ty else ""
    d["主营品类"] = model.get("mainCategoryName", "")

    # shopData: [{dataKey:"店铺回头率", dataValue:"78%", unit?}, ...]
    # 回头率/发货率/好评率带 % 保留; 带"分"单位的评分项只要数字 (去掉 unit "分")。
    _NO_UNIT_KEYS = {"店铺服务分", "采购咨询", "物流时效", "纠纷解决", "品质体验", "退换体验"}
    for it in model.get("shopData", []) or []:
        key = it.get("dataKey", "")
        val = str(it.get("dataValue", ""))
        if key not in _NO_UNIT_KEYS:
            val += (it.get("unit", "") or "")
        if key:
            d[key] = val

    # 店铺标签: iconType / shopType (cjgc -> 超级工厂)。未知类型回退原始值, 不静默丢。
    icon = model.get("iconType") or model.get("shopType") or ""
    d["店铺标签"] = ICON_MAP.get(icon, icon)
    return bool(d["店铺名称"])


def _main_category_from_html(page, html):
    """单独提取「主营品类」(shopcard JSON 缺 mainCategoryName 时用)。
    依次尝试: .shop-category-name 元素 → 任意元素里的「主营：xxx」文本 → 去 <style> 后正则。
    都没有返回空串。"""
    cat = _txt(_first(page, '.shop-category-name')).replace('主营：', '').replace('主营:', '').strip()
    if cat:
        return cat
    cm = re.search(r'主营[：:]\s*([^\n]+)', _txt(_first(page, '.shop-icon-list')))
    if cm:
        return cm.group(1).strip()
    # 兜底: 去掉 <style> 块后在整页 HTML 里找「主营：xxx」(避免命中 CSS/脚本里的字面量)
    dom = re.sub(r'<style\b[^>]*>.*?</style>', '', html, flags=re.S | re.I)
    m = re.search(r'主营[：:]\s*</?[^>]*>?\s*([^<\n，,；;]{1,40})', dom)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


def _shop_from_html(d, page, html):
    """JSON 缺失时的兜底: 从 HTML 解析店铺字段。"""
    d["店铺名称"] = _txt(_first(page, '.shop-company-name'))

    shop_url = page.css('.shop-company-name a::attr(href)').get()
    if not shop_url:
        m2 = re.search(r'(?:https?:)?//([a-z0-9-]+\.1688\.com)(?:/page/index\.html|["?])', html, re.I)
        shop_url = ('https://' + m2.group(1)) if m2 else ""
    shop_url = (shop_url or "").split('?')[0]
    if shop_url.startswith('//'):
        shop_url = 'https:' + shop_url
    d["店铺url"] = shop_url

    tags = [kw for kw in ['超级工厂', '源头工厂', '源头旗舰', '实力商家', '实力工厂',
                          '金牌制造', '认证工厂', '工厂直供', '严选'] if kw in html]
    d["店铺标签"] = "/".join(dict.fromkeys(tags))

    # 入驻年限: 去掉"入驻"前缀, 单位连页面一起取(不写死"年")。
    ym = re.search(r'入驻\s*(\d+\s*[^\d\s，,；;]+)', _txt(_first(page, '.shop-tp-year')) or "") \
        or re.search(r'入驻\s*(\d+\s*[^\d\s，,；;]+)', _txt(_first(page, '.shop-icon-list')) or "")
    d["入驻年限"] = ym.group(1).strip() if ym else ""

    d["主营品类"] = _main_category_from_html(page, html)

    _NO_UNIT = {"店铺服务分"}
    for it in page.css('.shop-data-item'):
        parts = [p for p in re.split(r'\n+', _txt(it)) if p.strip()]
        if len(parts) >= 2:
            key = parts[0]
            val = "".join(parts[1:])
            if key in _NO_UNIT:
                val = re.sub(r'[^\d.]', '', val)
            d[key] = val


def extract_detail(html, offer_id=None, detail_url=None, shop_json=None):
    page = Selector(html)
    d = {"offer_id": offer_id or "", "detail_url": detail_url or ""}

    # ---------- 商品信息 (HTML) ----------
    title = _txt(_first(page, '.module-od-title .title-content')) \
        or _txt(_first(page, '.title-content')) \
        or _txt(_first(page, '.offer-title'))
    if not title:
        raw = (page.css('title::text').get() or "").strip()
        title = re.sub(r'\s*[-—]\s*阿里巴巴.*$', '', raw)
    d["商品标题"] = title

    # 已售数量: <span>已售</span><span>2100+</span><span>双</span> -> "2100+双"
    m_sold = re.search(r'<span>已售</span><span>([^<]+)</span><span>([^<]+)</span>', html)
    if m_sold:
        # "2100+双" -> 取数字部分, 万级换算: "37万+" -> 370000
        raw_num = m_sold.group(1).strip()
        if '万' in raw_num:
            n = re.search(r'[\d.]+', raw_num)
            d["已售数量"] = str(int(float(n.group()) * 10000)) if n else ""
        else:
            n = re.search(r'\d+', raw_num)
            d["已售数量"] = n.group() if n else ""
    else:
        d["已售数量"] = ""

    # 价格: 文本形如 "价格\n¥\n17\n.00", 数字被换行拆开 -> 先去换行再正则取完整小数。
    price_el = _first(page, '.price-comp') or _first(page, '[class*="main-price"]') \
        or _first(page, '[class*="price-container"]')
    price_raw = _txt(price_el).replace('\n', '')
    m = re.search(r'¥?\s*(\d+(?:\.\d+)?)', price_raw)
    d["价格"] = m.group(1) if m else ""

    # 商品属性 (key-value): 1688 有两种结构, 都解析并合并:
    #   A) 表格式: .normal-attributes-table 里 <tr><td>键</td><td>值</td>
    #   B) 卡片式: .decision-attributes-list 里 <li><p>键</p><p>值</p> (如鞋类商品)
    attrs = {}
    for r in page.css('.normal-attributes-table tr'):
        cells = [c for c in (c.get_all_text(strip=True) for c in r.css('td')) if c]
        if len(cells) >= 2:
            attrs.setdefault(cells[0], cells[1])
    for li in page.css('.decision-attributes-list li'):
        ps = [t for t in (pp.get_all_text(strip=True) for pp in li.css('p')) if t]
        if len(ps) >= 2:
            attrs.setdefault(ps[0], ps[1])
    # C) ant-descriptions 表格: <th>key</th><td><span><span class="field-value">val</span></span></td>
    for row in page.css('.ant-descriptions-row'):
        ths = row.css('th.ant-descriptions-item-label')
        tds = row.css('td.ant-descriptions-item-content')
        for th, td in zip(ths, tds):
            key = th.get_all_text(strip=True)
            val = _txt(_first(td, '.field-value')) or td.get_all_text(strip=True)
            if key and val:
                attrs.setdefault(key, val)
    if not attrs:
        # 兜底: core-attributes 纯文本两两配对
        core = _first(page, '.core-attributes')
        if core:
            parts = [p for p in _txt(core).split('\n') if p.strip()]
            for i in range(0, len(parts) - 1, 2):
                attrs.setdefault(parts[i], parts[i + 1])
    d["商品属性"] = "; ".join(f"{k}:{v}" for k, v in attrs.items())

    # 商品详情(文字)
    desc_el = _first(page, '.html-description') or _first(page, '.module-od-product-description')
    d["商品详情"] = _clean_desc(_txt(desc_el))[:5000]

    # ---------- 店铺信息: JSON 优先, HTML 兜底 ----------
    # 先把店铺字段默认置空, 保证列齐全
    for k in ["店铺名称", "店铺url", "店铺标签", "入驻年限", "主营品类",
              "店铺回头率", "店铺服务分", "采购咨询", "物流时效", "纠纷解决", "品质体验", "退换体验",
              "准时发货率", "店铺好评率", "所在地区", "成立时间", "已售数量"]:
        d.setdefault(k, "")

    filled = False
    if shop_json:
        filled = _shop_from_json(d, shop_json)
    if not filled:
        _shop_from_html(d, page, html)
    elif not d.get("主营品类"):
        # JSON 填了店铺主字段, 但 shopcard 的 mainCategoryName 实测会缺失(如服装类
        # 部分店铺), 此时单独用 HTML/正则补「主营品类」, 不重跑整段 HTML 兜底。
        d["主营品类"] = _main_category_from_html(page, html)

    # 入驻年限: 以页面 .shop-tp-year 的真实文本为准(单位连页面一起取, 年/月都不写死)。
    # 形如 "入驻10年" -> 去掉"入驻"前缀保留 "10年"。页面没有时保留上面的 JSON 兜底值。
    tp = _txt(_first(page, '.shop-tp-year'))
    m_tp = re.search(r'入驻\s*(\d+\s*[^\d\s，,；;]+)', tp)
    if m_tp:
        d["入驻年限"] = m_tp.group(1).strip()

    # 所在地区 / 成立时间 / 采购咨询等5项: 来自 hover 店铺名弹出的悬浮卡片 (纯前端渲染)。
    card = _parse_hover_card(html)
    d["所在地区"] = card.get("所在地区") or _txt(_first(page, '.location'))
    d["成立时间"] = card.get("成立时间", "")
    for k in ("采购咨询", "物流时效", "纠纷解决", "品质体验", "退换体验"):
        if card.get(k):
            d[k] = card[k]

    return d


def _parse_hover_card(html):
    """从 hover 悬浮卡片 HTML 解析店铺详情字段，返回 dict。卡片没渲染时各项为空串。

    结构: .seller-advance-info 区块内两列 ul/li:
      左列 (.seller-advance-info-left):  <li><span>所在地区</span><span>浙江 金华</span></li>
      右列 (.seller-advance-info-right): <li><span>采购咨询</span><span>3.5</span>...</li>
      成立时间特殊: <li><span>2021年3月</span><span>成立时间</span></li>  (值在前)

    注意: class="seller-advance-info-right" 会先出现在 <style> 块里 (CSS 规则),
    需跳过 <style> 块只在 DOM 部分搜索。
    """
    # 去掉所有 <style>...</style> 块，避免 CSS 规则干扰 regex
    dom = re.sub(r'<style\b[^>]*>.*?</style>', '', html, flags=re.S | re.I)

    result = {}
    # 所在地区: label 在前
    m = re.search(r'>所在地区</span>\s*<span[^>]*>([^<]+)</span>', dom)
    if m:
        result["所在地区"] = re.sub(r'\s+', ' ', m.group(1)).strip()
    # 成立时间: 值在前, label 在后
    m = re.search(r'<span[^>]*>([^<]*\d[^<]*)</span>\s*<span[^>]*>成立时间</span>', dom)
    if m:
        result["成立时间"] = m.group(1).strip()
    # 右列评分项: seller-advance-info-right 的 li 里第一个 span 是 label, 第二个 span 是数值
    m_block = re.search(r'class="seller-advance-info-right">(.*?)</ul>', dom, re.S)
    if m_block:
        for li_m in re.finditer(r'<li><span>([^<]+)</span><span[^>]*>([\d.]+)</span>', m_block.group(1)):
            result[li_m.group(1)] = li_m.group(2)
    return result


def extract_shop_established(html):
    """从店铺页 HTML 里解析成立时间，统一转换为 "xxxx年xx月" 格式。
    店铺页常见形式: "2025.03成立" / "2025.3成立" / "2025年03月成立"。
    找不到时返回空串。"""
    # 去掉 <style> 块避免干扰
    dom = re.sub(r'<style\b[^>]*>.*?</style>', '', html, flags=re.S | re.I)

    patterns = [
        # "2025.03成立" 或 "2025.3成立"（点分格式）
        r'(\d{4})\.(\d{1,2})\s*成立',
        # "2025年03月成立" 或 "2025年3月成立"（已是目标格式，直接取）
        r'(\d{4})年(\d{1,2})月\s*成立',
        # 带 HTML 标签包裹的，如 <span>2025.03</span><span>成立</span>
        r'(\d{4})\.(\d{1,2})</[^>]+>\s*<[^>]+>成立',
        r'(\d{4})年(\d{1,2})月</[^>]+>\s*<[^>]+>成立',
    ]
    for pat in patterns:
        m = re.search(pat, dom)
        if m:
            year, month = m.group(1), m.group(2)
            return f"{year}年{int(month):02d}月"
    return ""


def extract_main_category(html):
    """从店铺页 (offerlist.htm) 头部解析「主营类目」文字, 返回主营品类字符串。
    用于 shopcard JSON 缺 mainCategoryName、详情页也没解析到主营品类时的兜底
    (实测服装/家居类部分店铺会这样)。找不到返回空串。

    页面常见形式 (label 与 value 可能同标签或分标签, 中间可夹 HTML):
      主营类目：日式家居服 / 主营产品: xxx / 主营: xxx
    """
    # 去 <style>/<script> 块, 避免命中里面的字面量
    dom = re.sub(r'<(style|script)\b[^>]*>.*?</\1>', '', html, flags=re.S | re.I)
    # 标签优先「主营类目/主营产品/主营业务」, 再退到单独「主营」
    for label in ('主营类目', '主营产品', '主营业务', '主营'):
        # label 后可夹任意标签/冒号, 取到第一个换行/逗号/分号/标签收尾前的文本
        m = re.search(label + r'\s*[：:]?\s*(?:</[^>]+>\s*<[^>]+>\s*)?([^<\n，,；;|]{1,60})', dom)
        if m:
            val = m.group(1).strip()
            # 过滤误命中 (空、纯标点、把"主营"自身又抓进来)
            if val and val != '主营' and not re.fullmatch(r'[\s：:、,，;；]+', val):
                return val
    return ""


def extract_offerlist(html):
    """从商家商品列表页 (offerlist.htm) 提取:
      - total_count:   总商品数 (int or None)
      - main_category: 主营类目文字 (str, 兜底用; 解析不到为 "")
      - categories:    [{"name": ..., "count": ...}, ...] (所有类目下各分类)
      - products:      [{"name": ..., "price": ..., "sold": ...}, ...] (第一屏商品)

    HTML 结构 (实测 1688 offerlist.htm):
      总商品数: 共<label style="color:...">173</label>件相关产品
      分类: <div class="first-category"><label title="保温杯">保温杯</label><label>(122)</label></div>
      商品卡: img.main-picture 定位卡片, <p title="名称">, 红色 span 取价格, span[title="累计销量"] 取已售
    """
    page = Selector(html)

    # 总商品数: 共<label>xxx</label>件相关产品
    total_count = None
    m = re.search(r'共<label[^>]*>(\d+)</label>件相关产品', html)
    if not m:
        m = re.search(r'共\s*(\d+)\s*件相关产品', html)
    if m:
        total_count = int(m.group(1))

    # 分类及数量: 两种结构并存
    #   A) class="first-category": <label title="名称"> + <label>(数量)</label> [+ <label>▼</label>]
    #      labels[-1] 可能是▼图标label, 需找第一个含数字的label作为数量
    #   B) li.offerlist / li.newofferlist: 新式导航, 分类名在<a>文本, 数量在下拉区div[title]旁的<label>
    categories = []
    seen_cats = set()

    def _add_cat(name, count_txt):
        mc = re.search(r'\d+', count_txt)
        if name and mc and name not in seen_cats and name not in ('所有类目', '全部商品', '全部'):
            seen_cats.add(name)
            categories.append({"name": name, "count": int(mc.group())})

    # A) first-category
    for div in page.css('.first-category'):
        labels = div.css('label')
        if not labels:
            continue
        name = (labels[0].attrib.get('title') or labels[0].get_all_text(strip=True)).strip()
        # 数量在第一个含数字的label(跳过▼图标label)
        cnt_txt = ""
        for lb in labels[1:]:
            t = lb.get_all_text(strip=True)
            if re.search(r'\d', t):
                cnt_txt = t
                break
        _add_cat(name, cnt_txt)

    # B) li.offerlist / li.newofferlist: 分类名在<a>直接文本节点, 数量在下拉div内label
    for li in page.css('li.offerlist, li.newofferlist'):
        a = li.css('a')
        if not a:
            continue
        # <a> 的直接文本(去掉子元素文字): 取 get_all_text 后去掉已知噪声
        name = a[0].get_all_text(strip=True).strip()
        # 去掉 a 内 img/span 带来的空白, 通常<a>只有纯文字+一张箭头img, 文字就是分类名
        if not name or name in ('所有类目', '全部商品', '全部'):
            continue
        # 数量: 下拉区 div.category 内 div[title=name] 旁的 <label>
        cnt_txt = ""
        cat_div = li.css('.category, .category-level')
        for cd in cat_div:
            # 找 title 属性等于分类名的 div
            for d2 in cd.css('div[title]'):
                if d2.attrib.get('title', '').strip() == name:
                    lb = d2.css('label')
                    if lb:
                        cnt_txt = lb[0].get_all_text(strip=True)
                    break
            if cnt_txt:
                break
        _add_cat(name, cnt_txt)

    # 商品列表: 每张卡片含 img.main-picture; 从 img 向上找最近的卡片容器
    # 实测卡片是 img 的祖父 div (img -> div -> 卡片 div), 用 regex 按卡片块切分更可靠
    products = []
    # 按 img class="main-picture" 把 HTML 切成卡片块, 每块向后取到下一张卡片或末尾
    card_splits = [m2.start() for m2 in re.finditer(r'class="main-picture"', html)]
    for i, start in enumerate(card_splits):
        end = card_splits[i + 1] if i + 1 < len(card_splits) else start + 4000
        chunk = html[max(0, start - 200): end + 500]

        # 商品名称: <p title="...">
        name = ""
        mn = re.search(r'<p\s[^>]*title="([^"]{4,})"', chunk)
        if mn:
            name = mn.group(1).strip()

        # 价格: 整数和小数被拆成两个红色 span (font-size:24px 的大字 + 小字如 ".88")
        # 做法: 收集 ¥ 之后所有连续红色 span 的文本拼起来, 再正则取完整数字
        price = ""
        # 找到 ¥ span 的位置, 从那里开始收集后续红色 span
        yen_m = re.search(r'color:\s*rgb\(255,\s*41,\s*0\)[^>]*>[^<]*¥[^<]*</span>', chunk)
        price_chunk = chunk[yen_m.end():yen_m.end() + 600] if yen_m else chunk
        parts = []
        for sp in re.finditer(r'color:\s*rgb\(255,\s*41,\s*0\)[^>]*>([^<]*)</span>', price_chunk):
            txt = sp.group(1).strip()
            if not txt or txt in ('¥', '限时价', '新人价', '活动价'):
                break  # 遇到非数字标签说明价格区结束
            if re.search(r'[\d.]', txt):
                parts.append(txt)
            else:
                break
        if parts:
            raw_price = ''.join(parts)
            pm = re.search(r'[\d.]+', raw_price)
            price = pm.group() if pm else ""

        # 已售数量: span[title="累计销量"] -> "已售100+件"
        sold = ""
        ms2 = re.search(r'title="累计销量"[^>]*>已售([\d万.+]+)件?', chunk)
        if not ms2:
            ms2 = re.search(r'已售([\d万.+]+)件', chunk)
        if ms2:
            raw_s = ms2.group(1).strip().rstrip('+')
            if '万' in raw_s:
                n = re.search(r'[\d.]+', raw_s)
                sold = str(int(float(n.group()) * 10000)) if n else raw_s
            else:
                sold = re.sub(r'[^\d]', '', raw_s)

        if name or price:
            products.append({"name": name, "price": price, "sold": sold})

    return {
        "total_count": total_count,
        "main_category": extract_main_category(html),
        "categories": categories,
        "products": products,
    }


if __name__ == "__main__":
    import sys
    for fn in sys.argv[1:]:
        html = open(fn, encoding='utf-8').read()
        sj = None
        res = extract_detail(html, offer_id=fn, shop_json=sj)
        print('=' * 60, '\nFILE:', fn)
        for k, v in res.items():
            print(f'  {k}: {str(v)[:90]!r}')
