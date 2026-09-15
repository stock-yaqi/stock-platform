#!/usr/bin/env python3
"""
筹码集中筛选：股东户数持续下降（事实数据）+ 低位 + 缩量。这是能找到「有人在收筹码」的唯一硬证据，盘面形态分不清吸筹和出货。

回测（东财 F10 股东户数 2024-03 ~ 2026-09，5211 只，每期期末价，相对全市场同期超额）：
    户数环比下降 >= 15%           下一期 +2.82%（74 期 3179 笔），披露后再下一期 +1.53%
    户数环比下降 >= 10%           下一期 +2.04%
    连续两期下降 >= 5%            下一期 +1.71%，按期 11/16 为正
    户数下降 >=5% 且前十大占比上升 下一期 +2.22%
    户数环比上升 >= 20%（散户涌入） 下一期 -2.50%，按期 8/18；再下一期 -0.60%   → 作为回避标记
    注意：户数按季（部分公司按月）披露，滞后 1-2 个月；效果一部分发生在披露前，可交易的是「再下一期」那部分（+1.5% 左右）。

得分（0-4）：户数最新一期下降>=10% (1) + 连续两期下降>=5% (1) + 前十大占比上升 (1) + 低位且非放量（距 40 日低 <=10%、20 日跌、5 日均额 <1.2 倍）(1)
回避标记：最新一期户数上升 >= 20%

用法：
    python3 scan_chips.py                 # 全市场，输出 筹码集中_<日期>.csv，发邮件（一天一封）
    python3 scan_chips.py --no-email --input 股票池.csv
    python3 scan_chips.py --min-drop 15
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
        p = sorted([x for x in gd.get(c, []) if x.get("HOLDER_TOTAL_NUM")], key=lambda x: x["END_DATE"])
        if len(p) < 2:
            continue
        a, b = p[-1], p[-2]
        chg = a.get("TOTAL_NUM_RATIO")
        chg_prev = b.get("TOTAL_NUM_RATIO")
        t10, t10p = a.get("HOLD_RATIO_TOTAL"), b.get("HOLD_RATIO_TOTAL")
        rows.append({"code": c, "name": names.get(c, ""), "期末": a["END_DATE"][:10], "上期": b["END_DATE"][:10], "户数": a["HOLDER_TOTAL_NUM"],
                     "环比%": chg, "上期环比%": chg_prev, "前十大%": t10, "前十大变化": (t10 - t10p) if (t10 is not None and t10p is not None) else np.nan,
                     "人均流通股": a.get("AVG_FREE_SHARES"), "集中度": a.get("HOLD_FOCUS")})
    df = pd.DataFrame(rows)
    df["环比%"] = pd.to_numeric(df["环比%"], errors="coerce")
    df["上期环比%"] = pd.to_numeric(df["上期环比%"], errors="coerce")
    df = df.dropna(subset=["环比%"])

    with ProcessPoolExecutor() as ex:
        ps = [r for r in ex.map(price_stats, [(c, day) for c in df["code"]], chunksize=50) if r]
    px = pd.DataFrame(ps)
    df = df.merge(px, on="code", how="left")
    df = df[df["avg20"].fillna(0) >= args.min_amt]

    s1 = df["环比%"] <= -args.min_drop
    s2 = (df["环比%"] <= -5) & (df["上期环比%"] <= -5)
    s3 = df["前十大变化"] > 0
    s4 = (df["dist_low"] <= 10) & (df["r20"] < 0) & (df["vr"] < 1.2)
    df["得分"] = s1.astype(int) + s2.astype(int) + s3.astype(int) + s4.astype(int)
    df["回避"] = np.where(df["环比%"] >= 20, "户数大增≥20%", "")
    cand = df[(s1 | s2)].sort_values(["得分", "环比%"], ascending=[False, True])
    avoid = df[df["回避"] != ""].sort_values("环比%", ascending=False)

    out = cand.rename(columns={"code": "代码", "name": "名称", "close": "收盘", "dist_low": "距40日低%", "r20": "20日涨幅%", "vr": "5日均额/20日均额", "avg20": "20日均额亿"})
    out = out[["代码", "名称", "得分", "期末", "户数", "环比%", "上期环比%", "前十大%", "前十大变化", "集中度", "收盘", "距40日低%", "20日涨幅%", "5日均额/20日均额", "20日均额亿"]].round(2)
    tag = "" if pool_name == "全市场" else f"_{pool_name}"
    path = os.path.join(HERE, f"筹码集中{tag}_{day}.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")
    avoid.rename(columns={"code": "代码", "name": "名称"})[["代码", "名称", "期末", "户数", "环比%", "上期环比%"]].round(2).to_csv(os.path.join(HERE, f"户数大增回避{tag}_{day}.csv"), index=False, encoding="utf-8-sig")

    lines = [f"{day} 筹码集中（{pool_name}）：样本 {len(df)} 只，户数下降候选 {len(cand)} 只（得分 4 的 {int((cand['得分'] == 4).sum())} 只，3 的 {int((cand['得分'] == 3).sum())} 只），户数大增回避 {len(avoid)} 只", ""]
    for r in out.head(args.top).itertuples():
        lines.append(f"  [{r.得分}] {r.代码} {r.名称:<6} 期末 {r.期末}  户数 {r.户数:,}  环比 {r._6:+.1f}%  上期 {r._7:+.1f}%  前十大 {r._8}%({r.前十大变化:+.1f})  收 {r.收盘}  距40日低 {r._12:+.1f}%  20日 {r._13:+.1f}%  量比 {r._14:.2f}")
    lines += ["", "户数大增回避（前 15）：" + "、".join(f"{r.name}({r.环比:+.0f}%)" for r in avoid.head(15).rename(columns={"环比%": "环比"}).itertuples()) if len(avoid) else "户数大增回避：无",
              "", "口径：得分 = 最新一期户数降≥10% + 连续两期降≥5% + 前十大占比上升 + 低位缩量。回测户数降≥15% 下一期超额 +2.8%、披露后 +1.5%；户数增≥20% 下一期 -2.5%。季度数据有滞后，作为中期筹码线索，不是短线信号。"]
    body = "\n".join(lines)
    parts = [L.h_section(f"筹码集中 · {pool_name}", f"候选 {len(cand)} 只 · 4 分 {int((cand['得分'] == 4).sum())} 只")]
    for r in out.head(args.top).itertuples():
        parts.append(L.h_card(f"{r.名称} <span style='color:{L.GRAY};font-weight:400;font-size:12px'>{r.代码}</span>", f"<b>{r.得分}</b><span style='color:{L.GRAY};font-size:12px'>/4</span>",
                              L.h_kv(("户数环比", L.h_pct(-r._6) if False else f"<span style='color:{L.GREEN if r._6 < 0 else L.RED};font-weight:600'>{r._6:+.1f}%</span>"), ("上期", f"{r._7:+.1f}%"), ("前十大", f"{r._8}%（{r.前十大变化:+.1f}）"), ("期末", r.期末)),
                              L.h_kv(("收盘", f"{r.收盘:g}"), ("距40日低", f"{r._12:+.1f}%"), ("20日", L.h_pct(r._13, 1)), ("量比", f"{r._14:.2f}"))))
    if len(avoid):
        parts.append(L.h_section("户数大增 · 回避", f"{len(avoid)} 只") + "<div style='padding:8px 0;line-height:1.8'>" + "、".join(f"{r.name} <span style='color:{L.RED}'>{r.环比:+.0f}%</span>" for r in avoid.head(20).rename(columns={'环比%': '环比'}).itertuples()) + "</div>")
    html = L.h_wrap(f"筹码集中 · {day[:4]}-{day[4:6]}-{day[6:]}", [f"{pool_name} · 样本 {len(df)} 只 · 股东户数为东财 F10 最新披露"], parts,
                    "得分 = 最新一期户数降≥10% + 连续两期降≥5% + 前十大占比上升 + 低位缩量。回测：户数降≥15% 下一期超额 +2.8%、披露后再下一期 +1.5%；户数增≥20% 下一期 -2.5%。季度数据滞后 1-2 个月，是中期筹码线索，不是短线信号。")
    print(body)
    print("已保存：", path)
    marker = os.path.join(L.META, f"chips_sent_{day}{tag}")
    if not args.no_email and len(cand):
        if os.path.exists(marker) and not args.resend:
            print("今天已发过，跳过发信（--resend 重发）")
            return
        if L.send_mail(f"【筹码集中】{day} {pool_name} 候选 {len(cand)} 只，4 分 {int((cand['得分'] == 4).sum())} 只；户数大增回避 {len(avoid)} 只" + ("（修正重发）" if os.path.exists(marker) else ""), body, html):
            open(marker, "w").write(datetime.now().strftime("%H:%M"))


if __name__ == "__main__":
    main()
