#!/usr/bin/env python3
"""
低位反转候选（收盘后）：不是「吸筹」形态识别，是回测里唯一站得住的低位均值回归条件。

回测（2026-06-25 ~ 09-01，日均成交额 >= 1 亿的 3691 只，相对全市场同期超额，持有 10 / 20 个交易日）：
    低位基础：收盘在 40 日最低价 5% 以内，20 日涨幅为负，且今天不是新低          10 日 +2.66%  20 日 +3.69%  10 日胜率 62%  按日 31/40 天为正
    + 深跌（20 日跌 >= 15%）+ 换手（5 日均换手 1%-8%）+ 非放量（5 日均额 < 1.2 倍 20 日均额）
                                                                                  10 日 +6.82%  20 日 +8.15%  胜率 75% / 66%  按日 21/21、13/15
    + 触发（当日涨 > 2%）                                                          10 日 +7.84%  胜率 78%（样本 219 笔 / 12 天，偏少）
    经典「吸筹」特征（温和放量、低点大单、涨时量大、收盘上半部）单独或合成都没有预测力，放量甚至为负；
    对照：高位（距 40 日高 5% 内）当日涨 >2%，10 日 -1.56%。
    注意：这是 2-4 周的均值回归，不是隔夜打法；样本只有 41 个交易日、一种行情，先小仓验证。

用法：
    python3 scan_low.py                       # 全市场活跃股，输出 低位候选_<日期>.csv，发邮件（一天一封）
    python3 scan_low.py --no-email
    python3 scan_low.py --input 国务院国资委控股_破净股_20260915.csv   # 只看某个股票池（需含「代码」列）
    python3 scan_low.py --top 40 --resend
"""
import argparse
import glob
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402

ROOT = L.ROOT


def stock_stats(args):
    code, day = args
    files = sorted(f for f in glob.glob(os.path.join(ROOT, code, "*.csv")) if os.path.basename(f)[:8] <= day)[-41:]
    if len(files) < 30 or os.path.basename(files[-1])[:8] != day:
        return None
    rows = []
    for f in files:
        try:
            x = pd.read_csv(f, usecols=["open", "high", "low", "close", "amount"])
        except Exception:
            return None
        if len(x) < 100:
            continue
        rows.append((os.path.basename(f)[:8], float(x["open"].iloc[0]), float(x["high"].max()), float(x["low"].min()), float(x["close"].iloc[-1]), float(x["amount"].sum())))
    d = pd.DataFrame(rows, columns=["d", "o", "h", "l", "c", "amt"])
    if len(d) < 25:
        return None
    t = d.iloc[-1]
    avg20 = d["amt"].iloc[-21:-1].mean()
    avg5 = d["amt"].iloc[-5:].mean()
    low40 = d["l"].min()
    high40 = d["h"].max()
    if avg20 <= 0 or low40 <= 0:
        return None
    return {"code": code, "close": t["c"], "ret": (t["c"] / d["c"].iloc[-2] - 1) * 100, "r20": (t["c"] / d["c"].iloc[-21] - 1) * 100 if len(d) > 21 else np.nan,
            "dist_low": (t["c"] / low40 - 1) * 100, "dist_high": (t["c"] / high40 - 1) * 100, "new_low": bool(t["c"] <= low40),
            "vr": avg5 / avg20, "amt": t["amt"] / 1e8, "avg20": avg20 / 1e8, "avg5": avg5,
            "pos5": float(((d["c"] - d["l"]) / (d["h"] - d["l"]).replace(0, np.nan)).iloc[-5:].mean())}


def main():
    ap = argparse.ArgumentParser(description="低位反转候选")
    ap.add_argument("--input", help="股票池 CSV（需含「代码」列），默认全市场")
    ap.add_argument("--date", help="YYYYMMDD，默认本地最新交易日")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--resend", action="store_true")
    ap.add_argument("--min-amt", type=float, default=1.0, help="20 日均成交额下限（亿）")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(ROOT, "000001", "*.csv")))
    day = args.date or days[-1]
    if args.input:
        pool = pd.read_csv(args.input, dtype=str)
        codes = [c.zfill(6) for c in pool["代码"]]
        pool_name = os.path.splitext(os.path.basename(args.input))[0]
    else:
        codes = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "[0-9]*")) if not os.path.basename(p).startswith("92"))
        pool_name = "全市场"
    with ProcessPoolExecutor() as ex:
        res = [r for r in ex.map(stock_stats, [(c, day) for c in codes], chunksize=50) if r]
    df = pd.DataFrame(res)
    df = df[df["avg20"] >= args.min_amt].dropna(subset=["r20"])

    # 换手率：流通市值取腾讯当前值近似
    q = L.batch_quotes(df["code"].tolist())
    names = {s["code"]: s["name"] for s in json.load(open(os.path.join(L.META, "stocklist.json")))["stocks"]}
    df["name"] = df["code"].map(names)
    fmc = {}
    syms = df["code"].tolist()
    import requests
    for i in range(0, len(syms), 80):
        try:
            r = L.http.get("https://qt.gtimg.cn/q=" + ",".join(L.tencent_symbol(c) for c in syms[i:i + 80]), headers=L.UA, timeout=20)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                if "=" not in line:
                    continue
                f = line.split("=")[1].strip('"\n ').split("~")
                if len(f) > 50:
                    try:
                        fmc[f[2]] = float(f[44]) * 1e8
                    except ValueError:
                        pass
        except Exception:
            pass
    df["fmc"] = df["code"].map(fmc)
    df["turn5"] = df["avg5"] / df["fmc"] * 100

    base = (df["dist_low"] <= 5) & (df["r20"] < 0) & (~df["new_low"])
    deep = df["r20"] <= -15
    turn = (df["turn5"] >= 1) & (df["turn5"] < 8)
    novol = df["vr"] < 1.2
    trig = df["ret"] > 2
    df["得分"] = base.astype(int) * (deep.astype(int) + turn.astype(int) + novol.astype(int))
    df["触发"] = np.where(base & trig, "当日涨>2%", "")
    df["级别"] = np.select([base & deep & turn & novol, base & (df["得分"] >= 2), base], ["1 深跌+换手+非放量", "2 两项", "3 仅低位"], "")
    cand = df[base].copy()
    cand = cand.sort_values(["级别", "触发", "r20"], ascending=[True, False, True])
    out = cand.rename(columns={"code": "代码", "name": "名称", "close": "收盘", "ret": "今日%", "r20": "20日涨幅%", "dist_low": "距40日低%", "dist_high": "距40日高%",
                               "vr": "5日均额/20日均额", "turn5": "5日均换手%", "amt": "今日成交额亿", "avg20": "20日均额亿", "pos5": "5日收盘位置"})
    out = out[["代码", "名称", "级别", "触发", "收盘", "今日%", "20日涨幅%", "距40日低%", "距40日高%", "5日均额/20日均额", "5日均换手%", "今日成交额亿", "20日均额亿", "5日收盘位置"]].round(2)
    tag = "" if pool_name == "全市场" else f"_{pool_name}"
    path = os.path.join(HERE, f"低位候选{tag}_{day}.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")

    lv1 = out[out["级别"].str.startswith("1")]
    lines = [f"{day} 低位反转候选（{pool_name}）：样本 {len(df)} 只，低位基础 {len(cand)} 只，其中 1 级 {len(lv1)} 只", ""]
    show = out.head(args.top)
    for r in show.itertuples():
        lines.append(f"  [{r.级别[:1]}] {r.代码} {r.名称:<6} 收 {r.收盘:<7} 今日 {r._6:+.2f}%  20日 {r._7:+.1f}%  距低 {r._8:+.1f}%  距高 {r._9:+.1f}%  量比 {r._10:.2f}  换手 {r._11:.1f}%  {r.触发}")
    lines += ["", "口径：1 级 = 距 40 日低 5% 内、20 日跌 15% 以上、5 日均换手 1-8%、5 日均额 < 1.2 倍 20 日均额、非当日新低。回测持有 10 日超额 +6.8%、20 日 +8.2%，胜率 75%/66%，41 个交易日样本。",
              "这是 2-4 周的均值回归，不是隔夜打法；创新低当天不买。仅筛选，非建议。"]
    body = "\n".join(lines)
    parts = [L.h_section(f"低位候选 · {pool_name}", f"低位基础 {len(cand)} 只 · 1 级 {len(lv1)} 只")]
    for r in show.itertuples():
        badge = "触发" if r.触发 else ""
        parts.append(L.h_card(f"{r.名称} <span style='color:{L.GRAY};font-weight:400;font-size:12px'>{r.代码}</span>", f"<span style='font-size:12px;color:{L.GRAY}'>{r.级别}</span>",
                              L.h_kv(("收盘", f"{r.收盘:g}"), ("今日", L.h_pct(r._6)), ("20日", L.h_pct(r._7, 1)), ("距40日低", f"{r._8:+.1f}%")),
                              L.h_kv(("量比", f"{r._10:.2f}"), ("5日均换手", f"{r._11:.1f}%"), ("20日均额", f"{r._13:.1f}亿"), ("距40日高", f"{r._9:+.1f}%")), badge))
    html = L.h_wrap(f"低位反转候选 · {day[:4]}-{day[4:6]}-{day[6:]}", [f"{pool_name} · 样本 {len(df)} 只"], parts,
                    "1 级 = 距 40 日低 5% 内、20 日跌 ≥15%、5 日均换手 1-8%、5 日均额 < 1.2 倍 20 日均额、非当日新低。回测持有 10 日超额 +6.8%、20 日 +8.2%，胜率 75%/66%（41 个交易日）。2-4 周均值回归，不是隔夜打法；创新低当天不买。仅筛选，非建议。")
    print(body)
    print("已保存：", path)
    marker = os.path.join(L.META, f"low_sent_{day}{tag}")
    if not args.no_email and len(cand):
        if os.path.exists(marker) and not args.resend:
            print("今天已发过，跳过发信（--resend 重发）")
            return
        if L.send_mail(f"【低位候选】{day} {pool_name} 1 级 {len(lv1)} 只，低位共 {len(cand)} 只" + ("（修正重发）" if os.path.exists(marker) else ""), body, html):
            open(marker, "w").write(datetime.now().strftime("%H:%M"))


if __name__ == "__main__":
    main()
