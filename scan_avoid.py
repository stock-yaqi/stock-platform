#!/usr/bin/env python3
"""
收盘后回避名单：放量但价格没有跟上的股票，之后 5-10 天大概率跑输大盘。

回测（2026-04-27 ~ 08-25，日均成交额 >= 1 亿的 3691 只，相对全市场同期超额）：
    高位放量长上影   成交额 >= 2 倍 20 日均值，上影线 >= 3%，20 日涨幅 >= 20%    10 日超额 -4.81%，71% 跑输，33 天里 30 天为负
    高位放量滞涨     成交额 >= 2 倍，当天涨跌在 ±1% 内，20 日涨幅 >= 20%          10 日超额 -4.46%，69% 跑输
    放量长上影       成交额 >= 2 倍，上影线 >= 3%                                  10 日超额 -3.05%，65% 跑输
    放量滞涨/阴线    成交额 >= 2 倍，涨跌 ±1% 内 或 阴线且跌幅 < 3%                10 日超额 -2.4% ~ -3.0%
    放量大涨(警示)   成交额 >= 2 倍，涨幅 >= 5%                                    10 日超额 -3.13%，别追
    低位放量滞涨（20 日涨幅 < 0）是正的（+3.9%），不列入回避。

用法：
    python3 scan_avoid.py                  # 用本地 mins 最新一天数据，输出 回避名单_<日期>.csv 并发邮件
    python3 scan_avoid.py --no-email
    python3 scan_avoid.py --date 20260908
    python3 scan_avoid.py --watch 601868 000001   # 额外检查这些持仓/自选，命中单独标出（也可放在 watchlist.txt 一行一个）
依赖：mins/ 本地分钟库（daily_job.sh 在 mins_sync 之后调用）
"""
import argparse
import glob
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402

ROOT = L.ROOT


def stock_stats(args):
    code, day = args
    files = sorted(glob.glob(os.path.join(ROOT, code, "*.csv")))
    files = [f for f in files if os.path.basename(f)[:8] <= day][-22:]
    if len(files) < 21 or os.path.basename(files[-1])[:8] != day:
        return None
    rows = []
    for f in files:
        try:
            x = pd.read_csv(f, usecols=["open", "high", "close", "amount"])
        except Exception:
            return None
        if len(x) < 100:
            return None
        rows.append((os.path.basename(f)[:8], float(x["open"].iloc[0]), float(x["high"].max()), float(x["close"].iloc[-1]), float(x["amount"].sum())))
    d = pd.DataFrame(rows, columns=["d", "o", "h", "c", "amt"])
    t = d.iloc[-1]
    avg20 = d["amt"].iloc[-21:-1].mean()
    if avg20 <= 0:
        return None
    return {"code": code, "close": t["c"], "ret": (t["c"] / d["c"].iloc[-2] - 1) * 100, "ar": t["amt"] / avg20,
            "upper": (t["h"] / t["c"] - 1) * 100, "g20": (t["c"] / d["c"].iloc[0] - 1) * 100, "yin": t["c"] < t["o"],
            "amt": t["amt"] / 1e8, "avg20": avg20 / 1e8}


def classify(r):
    if r["ar"] < 2:
        return None
    high = r["g20"] >= 20
    flat = abs(r["ret"]) < 1
    if high and r["upper"] >= 3:
        return "1 高位放量长上影"
    if high and flat:
        return "2 高位放量滞涨"
    if r["upper"] >= 3 and r["g20"] >= 0:
        return "3 放量长上影"
    if (flat or (r["yin"] and r["ret"] > -3)) and r["g20"] >= 0:
        return "4 放量滞涨/阴线"
    if r["ret"] >= 5:
        return "5 放量大涨(勿追)"
    return None


def main():
    ap = argparse.ArgumentParser(description="收盘后回避名单")
    ap.add_argument("--date", help="YYYYMMDD，默认本地最新交易日")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--min-amt", type=float, default=1.0, help="20 日均成交额下限（亿）")
    ap.add_argument("--watch", nargs="*", default=[], help="额外关注的代码")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(ROOT, "000001", "*.csv")))
    day = args.date or days[-1]
    codes = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "[0-9]*")) if not os.path.basename(p).startswith("92"))
    with ProcessPoolExecutor() as ex:
        res = [r for r in ex.map(stock_stats, [(c, day) for c in codes], chunksize=50) if r]
    df = pd.DataFrame(res)
    df = df[df["avg20"] >= args.min_amt]
    df["级别"] = df.apply(classify, axis=1)
    hit = df.dropna(subset=["级别"]).copy()
    names = {s["code"]: s["name"] for s in __import__("json").load(open(os.path.join(L.META, "stocklist.json")))["stocks"]}
    hit["名称"] = hit["code"].map(names)
    hit = hit.sort_values(["级别", "ar"], ascending=[True, False])
    out = hit.rename(columns={"code": "代码", "close": "收盘", "ret": "涨幅%", "ar": "成交额倍数", "upper": "上影线%", "g20": "20日涨幅%", "amt": "成交额亿", "avg20": "20日均额亿"})
    out = out[["代码", "名称", "级别", "收盘", "涨幅%", "成交额倍数", "上影线%", "20日涨幅%", "成交额亿", "20日均额亿"]].round(2)
    path = os.path.join(HERE, f"回避名单_{day}.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")

    watch = set(args.watch)
    wf = os.path.join(HERE, "watchlist.txt")
    if os.path.exists(wf):
        watch |= {l.strip() for l in open(wf) if l.strip() and not l.startswith("#")}
    lines = [f"{day} 回避名单：活跃股 {len(df)} 只，命中 {len(hit)} 只（1、2 级最强，之后 10 天平均跑输大盘 4% 以上）", ""]
    for lvl, g in out.groupby("级别"):
        lines.append(f"■ {lvl}  {len(g)} 只")
        for r in g.head(args.top // 2 if lvl.startswith(("3", "4", "5")) else args.top).itertuples():
            lines.append(f"  {r.代码} {r.名称:<6} 收 {r.收盘:<8} 涨 {r._5:+.2f}%  成交额 {r.成交额倍数:.1f} 倍  上影 {r._7:.1f}%  20日涨幅 {r._8:+.0f}%  成交额 {r.成交额亿:.1f} 亿")
        lines.append("")
    if watch:
        w = out[out["代码"].isin(watch)]
        lines.append("■ 自选/持仓命中：" + ("、".join(f"{r.代码} {r.名称}（{r.级别}）" for r in w.itertuples()) if len(w) else "无"))
        lines.append("")
    lines.append("用法：名单里的股票 5-10 天内不追、持有的考虑减仓；低位（20 日涨幅为负）放量不在名单里，那是另一回事。")
    body = "\n".join(lines)
    print(body)
    print(f"已保存：{path}")
    if not args.no_email and len(hit):
        L.send_mail(f"【回避名单】{day} 高位放量 {int((hit['级别'].str[0] <= '2').sum())} 只，共 {len(hit)} 只", body)


if __name__ == "__main__":
    main()
