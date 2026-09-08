#!/usr/bin/env python3
"""
资金进场扫描：用本地 mins/ 分钟数据，找出成交额突然放大、买盘主动、连续净流入、但还没涨完的股票。

每只股票计算（今天 = 本地最新一个交易日）：
    成交额倍数      今日成交额 / 前 20 日平均成交额
    换手率          今日成交量 / 流通股本（流通股本取自腾讯行情，按当前值近似历史）
    换手倍数        今日换手率 / 前 20 日平均换手率
    净流入占比      分钟收盘价高于上一分钟视为主动买、低于视为主动卖，(买额-卖额)/成交额
    近5日净流入占比  最近 5 个交易日合计
    连续净流入天数   从今天往前数，净流入为正的连续天数
    尾盘30分占比    最后 30 分钟成交额占全天比例（尾盘抢筹）
    最大分钟量倍数   当日最大分钟成交量 / 当日分钟成交量中位数（大单突刺）
    涨幅 / 5日涨幅 / 20日涨幅

默认筛选条件（可用参数调整）：
    成交额倍数 >= 2，今日净流入占比 > 0，近5日净流入占比 > 0，20日涨幅 < 30%，今日成交额 >= 1 亿

用法：
    python3 scan_inflow.py                   # 创业板
    python3 scan_inflow.py --board kcb       # 科创板；zb 主板；all 全市场
    python3 scan_inflow.py --min-ratio 3 --max-gain20 20 --top 30
    python3 scan_inflow.py --no-filter       # 不筛选，输出全部股票的指标

输出：资金进场_<板块>_<日期>.csv（全部股票指标，含「命中」列），终端打印命中榜单。
依赖：pip install requests pandas numpy
"""
import argparse
import glob
import json
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "mins")
BOARDS = {"cyb": ("创业板", ("30",)), "kcb": ("科创板", ("68",)), "zb": ("主板", ("60", "00")),
          "bj": ("北交所", ("92", "43", "83", "87")), "all": ("全市场", ("60", "68", "00", "30", "92", "43", "83", "87"))}
LOOKBACK = 20


def day_stats(path):
    x = pd.read_csv(path)
    if len(x) < 100:
        return None
    prev = x["close"].shift(1).fillna(x["open"].iloc[0])
    up, dn = x["close"] > prev, x["close"] < prev
    return dict(amt=float(x["amount"].sum()), vol=float(x["volume_hand"].sum()) * 100,
                net=float(x["amount"][up].sum() - x["amount"][dn].sum()),
                c=float(x["close"].iloc[-1]), last30=float(x["amount"].iloc[-30:].sum()),
                maxmin=float(x["volume_hand"].max()), medmin=float(x["volume_hand"].median()))


def analyze(code):
    files = sorted(glob.glob(os.path.join(ROOT, code, "*.csv")))
    if len(files) < LOOKBACK + 6:
        return None
    today_f = files[-1]
    t = day_stats(today_f)
    if not t or t["amt"] <= 0:
        return None
    hist = [s for s in (day_stats(f) for f in files[-LOOKBACK - 1:-1]) if s]
    if len(hist) < LOOKBACK * 0.75:
        return None
    avg_amt = np.mean([s["amt"] for s in hist])
    avg_vol = np.mean([s["vol"] for s in hist])
    if avg_amt <= 0:
        return None
    last5 = hist[-4:] + [t]
    streak = 0
    for s in reversed(hist + [t]):
        if s["net"] > 0:
            streak += 1
        else:
            break
    return dict(
        代码=code, 交易日=os.path.basename(today_f)[:8],
        现价=t["c"], 涨幅=(t["c"] / hist[-1]["c"] - 1) * 100,
        **{"5日涨幅": (t["c"] / hist[-5]["c"] - 1) * 100 if len(hist) >= 5 else np.nan,
           "20日涨幅": (t["c"] / hist[0]["c"] - 1) * 100},
        今日成交额亿=t["amt"] / 1e8, 成交额倍数=t["amt"] / avg_amt,
        今日成交量股=t["vol"], 均量股=avg_vol,
        净流入占比=t["net"] / t["amt"] * 100, 今日净流入亿=t["net"] / 1e8,
        近5日净流入占比=sum(s["net"] for s in last5) / max(sum(s["amt"] for s in last5), 1) * 100,
        近5日净流入亿=sum(s["net"] for s in last5) / 1e8,
        连续净流入天数=streak,
        尾盘30分占比=t["last30"] / t["amt"] * 100,
        最大分钟量倍数=t["maxmin"] / max(t["medmin"], 1),
    )


def tencent_float_shares(codes):
    """流通股本（股）= 流通市值(亿)*1e8 / 现价，来自腾讯行情"""
    s = requests.Session()
    s.trust_env = False
    out = {}

    def sym(c):
        return ("sh" if c.startswith(("6", "9")) else "bj" if c.startswith(("4", "8")) else "sz") + c
    for i in range(0, len(codes), 60):
        q = ",".join(sym(c) for c in codes[i:i + 60])
        try:
            r = s.get("https://qt.gtimg.cn/q=" + q, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                if "=" not in line:
                    continue
                f = line.split("=")[1].strip('"\n ').split("~")
                if len(f) < 50:
                    continue
                try:
                    price, fmc = float(f[3]), float(f[44])
                    if price > 0 and fmc > 0:
                        out[f[2]] = {"名称": f[1], "流通股本": fmc * 1e8 / price, "流通市值亿": fmc}
                except ValueError:
                    pass
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description="资金进场扫描")
    ap.add_argument("--board", default="cyb", choices=BOARDS, help="板块：cyb 创业板(默认) kcb 科创板 zb 主板 bj 北交所 all 全市场")
    ap.add_argument("--min-ratio", type=float, default=2.0, help="成交额倍数下限，默认 2")
    ap.add_argument("--min-amount", type=float, default=1.0, help="今日成交额下限（亿），默认 1")
    ap.add_argument("--max-gain20", type=float, default=30.0, help="20 日涨幅上限（%%），默认 30")
    ap.add_argument("--top", type=int, default=30, help="终端打印前几名，默认 30")
    ap.add_argument("--no-filter", action="store_true", help="不筛选，命中列仍会标注")
    ap.add_argument("-w", "--workers", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    board_name, prefixes = BOARDS[args.board]
    codes = sorted(os.path.basename(d) for d in glob.glob(os.path.join(ROOT, "[0-9]*")) if os.path.basename(d).startswith(prefixes))
    print(f"{board_name} {len(codes)} 只，回看 {LOOKBACK} 日，计算中...", flush=True)

    with ProcessPoolExecutor(args.workers) as ex:
        rows = [r for r in ex.map(analyze, codes, chunksize=20) if r]
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("没有可用数据，先运行 mins_sync.py")

    info = tencent_float_shares(df["代码"].tolist())
    df["名称"] = df["代码"].map(lambda c: info.get(c, {}).get("名称", ""))
    df["流通市值亿"] = df["代码"].map(lambda c: info.get(c, {}).get("流通市值亿", np.nan))
    fs = df["代码"].map(lambda c: info.get(c, {}).get("流通股本", np.nan))
    df["换手率"] = df["今日成交量股"] / fs * 100
    df["换手倍数"] = df["今日成交量股"] / df["均量股"]
    df["均换手率"] = df["均量股"] / fs * 100
    df.drop(columns=["今日成交量股", "均量股"], inplace=True)

    hit = ((df["成交额倍数"] >= args.min_ratio) & (df["净流入占比"] > 0) & (df["近5日净流入占比"] > 0)
           & (df["20日涨幅"] < args.max_gain20) & (df["今日成交额亿"] >= args.min_amount))
    df["命中"] = np.where(hit, "是", "")
    df["得分"] = (df["成交额倍数"] * (1 + df["近5日净流入占比"].clip(lower=0) / 100)
                * (1 + df["净流入占比"].clip(lower=0) / 100) * (1 + df["连续净流入天数"] / 10))
    cols = ["代码", "名称", "交易日", "命中", "得分", "现价", "涨幅", "5日涨幅", "20日涨幅", "今日成交额亿", "成交额倍数",
            "换手率", "均换手率", "换手倍数", "流通市值亿", "净流入占比", "今日净流入亿", "近5日净流入占比", "近5日净流入亿",
            "连续净流入天数", "尾盘30分占比", "最大分钟量倍数"]
    df = df[cols].sort_values(["命中", "得分"], ascending=[False, False]).round(2)

    day = df["交易日"].iloc[0]
    out = os.path.join(HERE, f"资金进场_{board_name}_{day}.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    show = df if args.no_filter else df[df["命中"] == "是"]
    pd.set_option("display.width", 250)
    print(f"\n{board_name} {day} 命中 {int(hit.sum())} 只（成交额倍数>={args.min_ratio}，净流入为正，近5日净流入为正，"
          f"20日涨幅<{args.max_gain20:.0f}%，成交额>={args.min_amount:.0f}亿），前 {args.top}：")
    print(show.head(args.top)[["代码", "名称", "得分", "涨幅", "5日涨幅", "20日涨幅", "今日成交额亿", "成交额倍数", "换手率", "换手倍数",
                              "净流入占比", "近5日净流入占比", "连续净流入天数", "尾盘30分占比"]].to_string(index=False))
    print(f"\n已保存：{out}")


if __name__ == "__main__":
    main()
