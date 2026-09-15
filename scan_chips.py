#!/usr/bin/env python3
"""
筹码集中筛选：股东户数持续下降（事实数据），按「披露后一期」可交易窗口的回测定级。盘面形态分不清吸筹和出货。

回测（东财 F10 股东户数 2024-03 ~ 2026-09，5211 只，每期期末价，披露后再下一期相对全市场超额）：
    1 级  户数降>=10% + 无涌入退潮 + 前十大占比 +>=1 个百分点   +2.53%  胜率 45%  9 期里 6 期为正（n=574）
    2 级  户数降>=10% + 无涌入退潮                              +1.50%  胜率 41%  20 期里 12 期为正（n=2887）；降>=15% 时 +1.84%
    退潮  户数降>=10% 但前 6 期涌入过>=20% 或户数仍高于前期最低 1.5 倍   期均超额中位 -1.0%，15 期里 7 期为正 -> 不列
    回避  户数升>=20%                                            -0.46%，21 期里 8 期为正
    另：户数在上涨中减少（期内涨幅>10%）不减分，回测反而更好（+2.3%~+2.7%）；前十大 +0~1 个百分点是噪音（+0.2%），要 >=1。
    当日创 40 日新低的暂不列（分钟数据回测：新低当天买入 10 日 -2.6%）。
    优势不大且逐期波动，需要分散 8-10 只；季度数据滞后 1-2 个月。

得分 0-4 只作展示：① 降>=10% ② 无涌入退潮 ③ 前十大 +>=1 ④ 时机（非当日新低且距 40 日低 <=10%）。级别以 ①②③ 定。

用法：
    python3 scan_chips.py                 # 全市场，输出 筹码集中_<日期>.csv/.html，发邮件（一天一封）
    python3 scan_chips.py --no-email --input 股票池.csv
先跑 fetch_gdrs.py 更新股东户数（每周一次即可）。
"""
import argparse
import glob
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402
import html_table  # noqa: E402

ROOT = L.ROOT


def price_stats(args):
    code, day = args
    files = sorted(f for f in glob.glob(os.path.join(ROOT, code, "*.csv")) if os.path.basename(f)[:8] <= day)[-41:]
    if len(files) < 25:
        return None
    rows = []
    for f in files:
        try:
            x = pd.read_csv(f, usecols=["low", "close", "amount"])
        except Exception:
            continue
        if len(x) >= 100:
            rows.append((float(x["low"].min()), float(x["close"].iloc[-1]), float(x["amount"].sum())))
    if len(rows) < 25:
        return None
    d = pd.DataFrame(rows, columns=["l", "c", "amt"])
    avg20 = d["amt"].iloc[-21:-1].mean()
    return {"code": code, "close": d["c"].iloc[-1], "dist_low": (d["c"].iloc[-1] / d["l"].min() - 1) * 100,
            "new_low": bool(d["l"].iloc[-1] <= d["l"].iloc[:-1].min()),
            "r20": (d["c"].iloc[-1] / d["c"].iloc[-21] - 1) * 100 if len(d) > 21 else np.nan,
            "vr": d["amt"].iloc[-5:].mean() / avg20 if avg20 > 0 else np.nan, "avg20": avg20 / 1e8}


def main():
    ap = argparse.ArgumentParser(description="筹码集中筛选（股东户数）")
    ap.add_argument("--input", help="股票池 CSV（需含「代码」列）")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--resend", action="store_true")
    ap.add_argument("--min-drop", type=float, default=10.0, help="最新一期户数环比下降幅度门槛(%)，默认 10")
    ap.add_argument("--min-amt", type=float, default=1.0, help="20 日均成交额下限（亿）")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    gd = json.load(open(os.path.join(L.META, "gdrs.json")))["stocks"]
    names = {s["code"]: s["name"] for s in json.load(open(os.path.join(L.META, "stocklist.json")))["stocks"]}
    days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(ROOT, "000001", "*.csv")))
    day = days[-1]
    if args.input:
        pool = [c.zfill(6) for c in pd.read_csv(args.input, dtype=str)["代码"]]
        pool_name = os.path.splitext(os.path.basename(args.input))[0]
    else:
        pool = [c for c in gd if not c.startswith("92")]
        pool_name = "全市场"

    rows = []
    for c in pool:
        p = sorted([x for x in gd.get(c, []) if x.get("HOLDER_TOTAL_NUM") and x.get("TOTAL_NUM_RATIO") is not None], key=lambda x: x["END_DATE"])
        if len(p) < 2:
            continue
        a, b = p[-1], p[-2]
        chg, chg_prev = a["TOTAL_NUM_RATIO"], b.get("TOTAL_NUM_RATIO")
        t10, t10p = a.get("HOLD_RATIO_TOTAL"), b.get("HOLD_RATIO_TOTAL")
        prev6 = [x.get("TOTAL_NUM_RATIO") or 0 for x in p[-7:-1]]
        hist_min = min(x["HOLDER_TOTAL_NUM"] for x in p[-9:-1])
        surge = max(prev6) if prev6 else 0
        rows.append({"code": c, "name": names.get(c, ""), "期末": a["END_DATE"][:10], "上期": b["END_DATE"][:10], "户数": a["HOLDER_TOTAL_NUM"],
                     "环比%": chg, "上期环比%": chg_prev, "前十大%": t10, "前十大变化": (t10 - t10p) if (t10 is not None and t10p is not None) else np.nan,
                     "前6期最大涌入%": surge, "户数/前期最低": a["HOLDER_TOTAL_NUM"] / hist_min if hist_min else np.nan,
                     "期内涨幅%": (a["PRICE"] / b["PRICE"] - 1) * 100 if (a.get("PRICE") and b.get("PRICE")) else np.nan, "期末价": a.get("PRICE"),
                     "人均流通股": a.get("AVG_FREE_SHARES"), "集中度": a.get("HOLD_FOCUS")})
    df = pd.DataFrame(rows)
    for col in ("环比%", "上期环比%", "前十大变化", "期内涨幅%", "期末价"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["环比%"])

    with ProcessPoolExecutor() as ex:
        ps = [r for r in ex.map(price_stats, [(c, day) for c in df["code"]], chunksize=50) if r]
    px = pd.DataFrame(ps)
    df = df.merge(px, on="code", how="left")
    df = df[df["avg20"].fillna(0) >= args.min_amt]
    df["期后涨幅%"] = (df["close"] / df["期末价"] - 1) * 100
    df["new_low"] = df["new_low"].fillna(False).astype(bool)
    df["dist_low"] = df["dist_low"].fillna(99)

    # 口径（披露后一期可交易窗口的回测，见文件头）
    drop = df["环比%"] <= -args.min_drop                                        # ① 户数降 >= 10%
    nosurge = (df["前6期最大涌入%"] < 20) & (df["户数/前期最低"] <= 1.5)          # ② 无涌入退潮
    top = df["前十大变化"] >= 1                                                  # ③ 前十大 +>=1 个百分点（+0~1 是噪音）
    timing = (~df["new_low"]) & (df["dist_low"] <= 10)                           # ④ 时机：非当日新低且在 40 日低点 10% 内（创新低当天买入回测为负）
    df["得分"] = drop.astype(int) + nosurge.astype(int) + top.astype(int) + timing.astype(int)
    df["级别"] = np.select([drop & nosurge & top, drop & nosurge, drop], ["1级 集中+机构", "2级 集中", "退潮"], "")
    df["标记"] = np.where(df["new_low"], "当日新低暂不", np.where(df["dist_low"] > 10, "已离开低位", ""))
    df["回避"] = np.where(df["环比%"] >= 20, "户数大增≥20%", "")
    cand = df[drop & nosurge].copy()
    cand = cand.sort_values(["级别", "标记", "环比%"], ascending=[True, True, True])
    tide = df[drop & ~nosurge].copy()
    avoid = df[df["回避"] != ""].sort_values("环比%", ascending=False)

    out = cand.rename(columns={"code": "代码", "name": "名称", "close": "收盘", "dist_low": "距40日低%", "r20": "20日涨幅%", "vr": "5日均额/20日均额", "avg20": "20日均额亿"})
    out = out[["代码", "名称", "级别", "得分", "标记", "期末", "户数", "环比%", "上期环比%", "前十大%", "前十大变化", "前6期最大涌入%", "户数/前期最低", "期内涨幅%", "期后涨幅%", "集中度", "收盘", "距40日低%", "20日涨幅%", "5日均额/20日均额", "20日均额亿"]].round(2)
    tide.rename(columns={"code": "代码", "name": "名称"})[["代码", "名称", "期末", "户数", "环比%", "前6期最大涌入%", "户数/前期最低"]].round(2).to_csv(os.path.join(HERE, f"户数退潮{tag if False else ''}_{day}.csv"), index=False, encoding="utf-8-sig")
    tag = "" if pool_name == "全市场" else f"_{pool_name}"
    path = os.path.join(HERE, f"筹码集中{tag}_{day}.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")
    avoid.rename(columns={"code": "代码", "name": "名称"})[["代码", "名称", "期末", "户数", "环比%", "上期环比%"]].round(2).to_csv(os.path.join(HERE, f"户数大增回避{tag}_{day}.csv"), index=False, encoding="utf-8-sig")

    n1 = int(out["级别"].str.startswith("1").sum()); n_ok = int(((out["标记"] == "")).sum())
    show = out[out["标记"] != "当日新低暂不"].head(args.top)
    lines = [f"{day} 筹码集中（{pool_name}）：样本 {len(df)} 只，候选 {len(cand)} 只（1 级 {n1} 只），退潮 {len(tide)} 只不列，户数大增回避 {len(avoid)} 只；候选中当日新低 {int((out['标记'] == '当日新低暂不').sum())} 只暂不列", ""]
    for r in show.itertuples():
        lines.append(f"  [{r.级别}] {r.代码} {r.名称:<6} {r.标记}  户数 {r.户数:,}（{r._8:+.1f}%，上期 {r._9:+.1f}%）前十大 {r._10}%({'—' if pd.isna(r.前十大变化) else f'{r.前十大变化:+.1f}'})  期内 {r._14:+.0f}% 期后 {r._15:+.0f}%  收 {r.收盘} 距40日低 {r._18:+.1f}% 量比 {r._20:.2f}")
    lines += ["", "户数大增回避（前 15）：" + "、".join(f"{r.name}({r.环比:+.0f}%)" for r in avoid.head(15).rename(columns={"环比%": "环比"}).itertuples()) if len(avoid) else "户数大增回避：无",
              "", "口径（披露后一期可交易窗口回测）：1 级 = 户数降≥10% + 无涌入退潮 + 前十大 +≥1 个百分点，超额 +2.5%、胜率 45%、9 期里 6 期为正；2 级 = 户数降≥10% + 无涌入退潮，+1.5%、20 期里 12 期为正；退潮（前 6 期涌入过≥20% 或户数仍高于前期最低 1.5 倍）不列；户数增≥20% 回避（-0.5%）。当日创 40 日新低的暂不列。季度数据滞后 1-2 个月，优势不大，需要分散 8-10 只。"]
    body = "\n".join(lines)
    parts = [L.h_section(f"筹码集中 · {pool_name}", f"候选 {len(cand)} 只 · 1 级 {n1} 只 · 退潮 {len(tide)} 只不列")]
    for r in show.itertuples():
        col = L.RED if r.级别.startswith("1") else "#b8742a"
        parts.append(L.h_card(L.h_name(r.名称, r.代码), f"<span style='font-size:12px;color:{col};font-weight:700'>{r.级别}</span>" + (f" <span style='font-size:11px;color:{L.GRAY}'>{r.标记}</span>" if r.标记 else ""),
                              L.h_kv(("户数", f"<span style='color:{L.GREEN};font-weight:600'>{r._8:+.1f}%</span>"), ("上期", f"{r._9:+.1f}%"), ("前十大", f"{r._10}%（{'—' if pd.isna(r.前十大变化) else f'{r.前十大变化:+.1f}'}）"), ("期末", r.期末)),
                              L.h_kv(("期内", L.h_pct(r._14, 0)), ("期后至今", L.h_pct(r._15, 0)), ("距40日低", f"{r._18:+.1f}%"), ("量比", f"{r._20:.2f}"), ("收盘", f"{r.收盘:g}"))))
    if len(avoid):
        parts.append(L.h_section("户数大增 · 回避", f"{len(avoid)} 只") + "<div style='padding:8px 0;line-height:1.8'>" + "、".join(f"<a href='{L.stock_url(r.code)}' style='color:{L.INK}'>{r.name}</a> <span style='color:{L.RED}'>{r.环比:+.0f}%</span>" for r in avoid.head(20).rename(columns={'环比%': '环比'}).itertuples()) + "</div>")
    html = L.h_wrap(f"筹码集中 · {day[:4]}-{day[4:6]}-{day[6:]}", [f"{pool_name} · 样本 {len(df)} 只 · 股东户数为东财 F10 最新披露"], parts,
                    "口径（披露后一期可交易窗口）：1 级 = 户数降≥10% + 无涌入退潮 + 前十大 +≥1 个百分点，超额 +2.5%、9 期里 6 期为正；2 级 = 户数降≥10% + 无涌入退潮，+1.5%、20 期里 12 期为正。退潮和户数增≥20% 的不列。当日创 40 日新低的暂不列。优势不大，需要分散 8-10 只，季度数据滞后 1-2 个月。")
    # 本地 HTML（自带数据、可排序筛选），随邮件作为附件发出
    industry = json.load(open(os.path.join(L.META, "industry_em.json")))
    def fnum(v):
        try:
            return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 2)
        except Exception:
            return None
    cand_rows = [{"s": int(r["得分"]), "c": r["代码"], "n": r["名称"], "i": industry.get(r["代码"], {}).get("em1", ""), "d": r["期末"], "h": int(r["户数"]),
                  "g": fnum(r["环比%"]), "gp": fnum(r["上期环比%"]), "t": fnum(r["前十大%"]), "td": fnum(r["前十大变化"]), "f": r["集中度"] if isinstance(r["集中度"], str) else "",
                  "p": fnum(r["收盘"]), "dl": fnum(r["距40日低%"]), "r20": fnum(r["20日涨幅%"]), "vr": fnum(r["5日均额/20日均额"]), "a": fnum(r["20日均额亿"]),
                  "lv": r["级别"], "mk": r["标记"], "ri": fnum(r["期内涨幅%"]), "ra": fnum(r["期后涨幅%"]),
                  "s1": bool(r["环比%"] <= -args.min_drop), "s2": bool((r["前6期最大涌入%"] or 0) < 20 and (r["户数/前期最低"] or 0) <= 1.5), "s3": bool((r["前十大变化"] or 0) >= 1),
                  "s4": bool(r["标记"] == "")} for r in out.to_dict("records")]
    avoid_rows = [{"c": r["code"], "n": r["name"], "i": industry.get(r["code"], {}).get("em1", ""), "d": r["期末"], "h": int(r["户数"]), "g": fnum(r["环比%"]), "gp": fnum(r["上期环比%"])} for r in avoid.to_dict("records")]
    period = out["期末"].max() if len(out) else ""
    html_path = os.path.join(HERE, f"筹码集中{tag}_{day}.html")
    open(html_path, "w", encoding="utf-8").write(html_table.render(cand_rows, avoid_rows, {"day": f"{day[:4]}-{day[4:6]}-{day[6:]}", "period": period, "pool": pool_name}))
    print(body)
    print("已保存：", path, "和", html_path)
    marker = os.path.join(L.META, f"chips_sent_{day}{tag}")
    if not args.no_email and len(cand):
        if os.path.exists(marker) and not args.resend:
            print("今天已发过，跳过发信（--resend 重发）")
            return
        if L.send_mail(f"【筹码集中】{day} {pool_name} 1 级 {n1} 只，候选共 {len(cand)} 只；退潮 {len(tide)} 只不列，回避 {len(avoid)} 只（附可筛选表格）" + ("（修正重发）" if os.path.exists(marker) else ""), body, html, attachments=[html_path]):
            open(marker, "w").write(datetime.now().strftime("%H:%M"))


if __name__ == "__main__":
    main()
