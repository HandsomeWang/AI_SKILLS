"""数据分析：合并多关键词采集结果 → 清洗去重 → 产出 5 维度分析 + Excel 汇总。

职责边界（重要）：
- 本脚本只产**客观信号**：数值、分布、占比、排名、归一化评分。
- **不做语义判断**。尤其"商家匹配度"里的"这家店是不是专做该品类"这种判断，关键词匹配会
  误判（需求"袜子"而类目叫"船袜/运动袜"），所以脚本只把每个商家整理成紧凑的、可供 AI
  判断的结构（主营品类 / 类目分布 / 商品名样本 / 命中商品 / 商家评分），语义匹配交给 AI。

产出：
  <workspace>/analysis.json   —— 给 AI 写报告用的结构化分析
  <workspace>/选品数据_<ts>.xlsx —— 多工作表：合并明细 / 价格 / 销量Top / 优质供应商 / 商家信号 / 属性热点

用法（用 .venv 的 python 跑）：
  <venv-python> scripts/analyze.py --inputs ws/1688_a.json ws/1688_b.json \\
      --workspace ws --demand "销量高的纯棉船袜" --timestamp 20260629_2100
"""
import os
import re
import json
import argparse
from collections import Counter

import pandas as pd


def json_default(o):
    """numpy/pandas 标量(int64/float64/bool_)不可直接 JSON 序列化，统一转 Python 原生。"""
    if hasattr(o, "item"):   # numpy 标量都支持 .item()
        return o.item()
    return str(o)


# ---------- 清洗辅助 ----------

def to_num(x):
    """从形如 '2.50' / '9300' / '96.3%' / '5.0' 的文本里取数值；取不到返回 None。"""
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    s = s.replace("%", "").replace(",", "").replace("万", "")  # '万' 极少见, 简单去掉
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def is_valid_row(rec):
    """过滤被拦截/错误行。"""
    t = str(rec.get("商品标题", ""))
    return t and not t.startswith("[")


def parse_json_field(s):
    """店铺商品分类 / 店铺商品列表 存的是 JSON 字符串，解析回 list；失败返回 []。"""
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def parse_attrs(s):
    """商品属性形如 '成分及含量:76%棉 21%聚酯纤维; 图案:纯色; 适用性别:男; ...' → dict。"""
    out = {}
    if not s:
        return out
    for part in str(s).split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, _, v = part.partition(":")
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


# ---------- 加载与合并 ----------

def load_merge(inputs):
    """读多份 1688_*.json，合并，按 offer_id 去重，过滤无效行。返回 list[dict]。"""
    seen = set()
    rows = []
    for path in inputs:
        if not os.path.exists(path):
            print(f"!! 输入文件不存在，跳过: {path}")
            continue
        data = json.load(open(path, encoding="utf-8"))
        for rec in data:
            if not is_valid_row(rec):
                continue
            oid = str(rec.get("offer_id", ""))
            if oid and oid in seen:
                continue
            seen.add(oid)
            rows.append(rec)
    return rows


# ---------- 5 维度分析 ----------

def analyze_price(df):
    p = df["价格_num"].dropna()
    if p.empty:
        return {"count": 0}
    q1, q2 = p.quantile(1 / 3), p.quantile(2 / 3)
    bands = {
        "低价带": f"≤{q1:.2f}",
        "中价带": f"{q1:.2f}~{q2:.2f}",
        "高价带": f">{q2:.2f}",
    }
    counts = {
        "低价带": int((p <= q1).sum()),
        "中价带": int(((p > q1) & (p <= q2)).sum()),
        "高价带": int((p > q2).sum()),
    }
    return {
        "count": int(p.count()),
        "min": round(float(p.min()), 2),
        "median": round(float(p.median()), 2),
        "mean": round(float(p.mean()), 2),
        "max": round(float(p.max()), 2),
        "价格带定义": bands,
        "各价格带商品数": counts,
    }


def analyze_sales(df, top_n=10):
    d = df.dropna(subset=["已售_num"]).sort_values("已售_num", ascending=False)
    top = [
        {
            "商品标题": r["商品标题"],
            "价格": r.get("价格", ""),
            "已售数量": int(r["已售_num"]),
            "店铺名称": r.get("店铺名称", ""),
            "detail_url": r.get("detail_url", ""),
        }
        for _, r in d.head(top_n).iterrows()
    ]
    # 量价关系：高销量(前1/3)集中在哪个价格带。
    band_of_top = {}
    if not d.empty and df["价格_num"].notna().any():
        p = df["价格_num"].dropna()
        q1, q2 = p.quantile(1 / 3), p.quantile(2 / 3)
        head = d.head(max(1, len(d) // 3))
        for _, r in head.iterrows():
            pv = r["价格_num"]
            if pd.isna(pv):
                continue
            band = "低价带" if pv <= q1 else ("中价带" if pv <= q2 else "高价带")
            band_of_top[band] = band_of_top.get(band, 0) + 1
    return {
        "top_products": top,
        "总销量": int(d["已售_num"].sum()) if not d.empty else 0,
        "高销量商品价格带分布": band_of_top,
    }


def analyze_supplier_quality(df, top_n=10):
    """按商家评分排优质店铺；同时统计店铺标签分布。"""
    # 一店一行（同店多商品取第一条店铺信息即可）。
    shops = df.drop_duplicates(subset=["店铺名称"]).copy()
    shops["好评_num"] = shops["店铺好评率"].map(to_num)
    shops["服务分_num"] = shops["店铺服务分"].map(to_num)
    shops["回头_num"] = shops["店铺回头率"].map(to_num)
    shops["发货_num"] = shops["准时发货率"].map(to_num)
    shops = shops.sort_values(
        ["服务分_num", "好评_num", "回头_num"], ascending=False, na_position="last"
    )
    top = [
        {
            "店铺名称": r.get("店铺名称", ""),
            "店铺标签": r.get("店铺标签", ""),
            "入驻年限": r.get("入驻年限", ""),
            "店铺好评率": r.get("店铺好评率", ""),
            "店铺服务分": r.get("店铺服务分", ""),
            "店铺回头率": r.get("店铺回头率", ""),
            "准时发货率": r.get("准时发货率", ""),
            "所在地区": r.get("所在地区", ""),
        }
        for _, r in shops.head(top_n).iterrows()
    ]
    tag_counter = Counter()
    for t in df["店铺标签"].fillna(""):
        for tag in re.split(r"[ ,;，；]+", str(t)):
            if tag.strip():
                tag_counter[tag.strip()] += 1
    return {"top_shops": top, "店铺标签分布": dict(tag_counter)}


def analyze_attributes(df, top_k=15):
    """统计高频商品属性（成分/款式/材质等卖点）。"""
    key_counter = Counter()
    val_counter = {}  # 属性名 -> Counter(取值)
    for s in df["商品属性"].fillna(""):
        for k, v in parse_attrs(s).items():
            key_counter[k] += 1
            val_counter.setdefault(k, Counter())[v] += 1
    hot = {}
    for k, _ in key_counter.most_common(top_k):
        hot[k] = dict(val_counter[k].most_common(5))
    return {"高频属性及取值": hot}


def normalize_shop_score(r):
    """把店铺各项评分归一化到 0~1 再加权，作为客观'商家评分'信号(非最终匹配度)。
    权重：服务分0.35 好评率0.30 回头率0.20 准时发货0.15。缺项按可用项重新归一。"""
    parts = []
    sf = to_num(r.get("店铺服务分"))   # 满分 5
    hp = to_num(r.get("店铺好评率"))   # 百分比
    ht = to_num(r.get("店铺回头率"))   # 百分比
    fh = to_num(r.get("准时发货率"))   # 百分比
    if sf is not None:
        parts.append((min(sf / 5.0, 1.0), 0.35))
    if hp is not None:
        parts.append((min(hp / 100.0, 1.0), 0.30))
    if ht is not None:
        parts.append((min(ht / 100.0, 1.0), 0.20))
    if fh is not None:
        parts.append((min(fh / 100.0, 1.0), 0.15))
    if not parts:
        return None
    wsum = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / wsum
    return round(score, 3)


def build_supplier_signals(df):
    """为每个商家整理 AI 做语义匹配判断所需的紧凑信号（脚本不下匹配结论）。"""
    suppliers = []
    for shop, g in df.groupby("店铺名称"):
        if not shop:
            continue
        first = g.iloc[0]
        cats = parse_json_field(first.get("店铺商品分类"))
        cats = sorted(cats, key=lambda c: c.get("count", 0) or 0, reverse=True)
        prods = parse_json_field(first.get("店铺商品列表"))
        prod_names = [p.get("name", "") for p in prods if p.get("name")][:15]
        hit_titles = [t for t in g["商品标题"].tolist() if t][:5]
        suppliers.append({
            "店铺名称": shop,
            "主营品类": first.get("主营品类", ""),
            "店铺标签": first.get("店铺标签", ""),
            "入驻年限": first.get("入驻年限", ""),
            "所在地区": first.get("所在地区", ""),
            "店铺总商品数": first.get("店铺总商品数", ""),
            "店铺商品分类_TopN": cats[:10],          # [{name,count}]，按 count 降序
            "店铺商品名样本": prod_names,             # 让 AI 看这家到底在卖什么
            "本次命中商品标题": hit_titles,           # 搜索结果里来自这家的商品
            "命中商品数": int(len(g)),
            # 客观商家评分（0~1），AI 结合品类语义匹配后给最终匹配度
            "商家评分": normalize_shop_score(first),
            "店铺好评率": first.get("店铺好评率", ""),
            "店铺服务分": first.get("店铺服务分", ""),
            "店铺回头率": first.get("店铺回头率", ""),
            "准时发货率": first.get("准时发货率", ""),
        })
    # 先按客观商家评分降序，方便 AI 浏览（最终匹配度排序由 AI 结合语义后定）。
    suppliers.sort(key=lambda s: (s["商家评分"] is not None, s["商家评分"] or 0), reverse=True)
    return suppliers


# ---------- Excel 导出 ----------

def export_excel(df, analysis, suppliers, path):
    from openpyxl import Workbook
    from openpyxl.utils.dataframe import dataframe_to_rows

    wb = Workbook()

    # 1) 合并明细（原始字段，去掉我们加的 *_num 辅助列）
    ws = wb.active
    ws.title = "合并明细"
    raw_cols = [c for c in df.columns if not c.endswith("_num")]
    ws.append(raw_cols)
    for _, r in df.iterrows():
        ws.append([r.get(c, "") for c in raw_cols])

    def add_sheet(title, headers, rows):
        s = wb.create_sheet(title)
        s.append(headers)
        for row in rows:
            s.append(row)

    # 2) 价格分析
    pr = analysis["价格分析"]
    add_sheet("价格分析", ["指标", "值"], [
        ["商品数", pr.get("count", 0)],
        ["最低价", pr.get("min", "")],
        ["中位价", pr.get("median", "")],
        ["均价", pr.get("mean", "")],
        ["最高价", pr.get("max", "")],
        ["低价带商品数", pr.get("各价格带商品数", {}).get("低价带", "")],
        ["中价带商品数", pr.get("各价格带商品数", {}).get("中价带", "")],
        ["高价带商品数", pr.get("各价格带商品数", {}).get("高价带", "")],
    ])

    # 3) 销量 Top
    add_sheet("销量Top",
              ["商品标题", "价格", "已售数量", "店铺名称", "detail_url"],
              [[t["商品标题"], t["价格"], t["已售数量"], t["店铺名称"], t["detail_url"]]
               for t in analysis["销量与爆款"]["top_products"]])

    # 4) 优质供应商
    add_sheet("优质供应商",
              ["店铺名称", "店铺标签", "入驻年限", "店铺好评率", "店铺服务分",
               "店铺回头率", "准时发货率", "所在地区"],
              [[s["店铺名称"], s["店铺标签"], s["入驻年限"], s["店铺好评率"],
                s["店铺服务分"], s["店铺回头率"], s["准时发货率"], s["所在地区"]]
               for s in analysis["供应商质量"]["top_shops"]])

    # 5) 商家信号（供 AI 判断匹配度的原料 + 客观商家评分）
    add_sheet("商家信号",
              ["店铺名称", "主营品类", "商家评分(客观)", "命中商品数", "店铺总商品数",
               "店铺商品分类_TopN", "店铺商品名样本", "本次命中商品标题", "店铺标签", "所在地区"],
              [[s["店铺名称"], s["主营品类"], s["商家评分"], s["命中商品数"],
                s["店铺总商品数"],
                "; ".join(f'{c.get("name","")}({c.get("count","")})' for c in s["店铺商品分类_TopN"]),
                " | ".join(s["店铺商品名样本"]),
                " | ".join(s["本次命中商品标题"]),
                s["店铺标签"], s["所在地区"]]
               for s in suppliers])

    # 6) 属性热点
    attr_rows = []
    for k, vals in analysis["属性热点"]["高频属性及取值"].items():
        attr_rows.append([k, "; ".join(f"{vk}({vc})" for vk, vc in vals.items())])
    add_sheet("属性热点", ["属性", "高频取值(出现次数)"], attr_rows)

    wb.save(path)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, help="一个或多个 1688_*.json")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--demand", default="", help="用户原始需求(透传进 analysis.json 供 AI 参考)")
    ap.add_argument("--timestamp", default="output", help="产物文件名时间戳")
    args = ap.parse_args()

    os.makedirs(args.workspace, exist_ok=True)

    rows = load_merge(args.inputs)
    if not rows:
        print("!! 合并后无有效商品数据（可能全部被拦截或无结果）。")
        # 仍写一个空 analysis.json，避免下游崩。
        out = {"demand": args.demand, "商品总数": 0, "提示": "无有效数据"}
        json.dump(out, open(os.path.join(args.workspace, "analysis.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2,
                  default=json_default)
        return

    df = pd.DataFrame(rows)
    df["价格_num"] = df["价格"].map(to_num)
    df["已售_num"] = df["已售数量"].map(to_num)

    analysis = {
        "demand": args.demand,
        "关键词文件": [os.path.basename(p) for p in args.inputs],
        "商品总数": int(len(df)),
        "去重说明": "已按 offer_id 去重，并过滤被拦截/错误行；数据仅含各关键词搜索页第一屏。",
        "价格分析": analyze_price(df),
        "销量与爆款": analyze_sales(df),
        "供应商质量": analyze_supplier_quality(df),
        "属性热点": analyze_attributes(df),
        # suppliers 是给 AI 做语义匹配判断的原料；脚本不下匹配结论。
        "suppliers": build_supplier_signals(df),
        "商家匹配度说明": (
            "suppliers 列表是给 AI 做语义匹配判断的客观信号。请 AI 据此对每个商家判断："
            "①品类匹配(高/中/低)——结合需求与主营品类/类目分布/商品名样本，判断这家是否专做该品类、"
            "搜到的商品是主力还是边缘；②再结合脚本算好的'商家评分'(0~1)，综合给出商家匹配度与排序。"
        ),
    }

    json_path = os.path.join(args.workspace, "analysis.json")
    json.dump(analysis, open(json_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=json_default)

    xlsx_path = os.path.join(args.workspace, f"选品数据_{args.timestamp}.xlsx")
    export_excel(df, analysis, analysis["suppliers"], xlsx_path)

    print(f"[analyze] 有效商品 {len(df)} 条，商家 {df['店铺名称'].nunique()} 家")
    print(f"[analyze] analysis.json -> {json_path}")
    print(f"[analyze] Excel -> {xlsx_path}")
    # 最后两行固定打印两个产物路径，供 skill 捕获。
    print(json_path)
    print(xlsx_path)


if __name__ == "__main__":
    main()
