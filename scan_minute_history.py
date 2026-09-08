#!/usr/bin/env python3
"""
回看股票池中每只股票近 N 个交易日的分钟级成交量，统计每天的最大分钟量以及超阈值的分钟数。

数据源：新浪 1 分钟 K 线（一次最多 1500 根，约 6 个交易日；无需 cookie、不封 IP）。
默认读取同目录下最新的 分钟放量_*.csv（scan_minute_volume.py 的输出），也可用 -i 指定任意含「代码」列的 CSV。

用法：
    python3 scan_minute_history.py                 # 最新 分钟放量 CSV，回看最近 5 个交易日（不含今天），阈值 5 万手
    python3 scan_minute_history.py -d 5 --include-today
    python3 scan_minute_history.py -t 30000 -i 国务院国资委控股_破净股_20260908.csv

输出：
    分钟放量历史_<日期>.csv  宽表：每只股票 × 每个交易日的最大分钟量(手)，以及超阈值天数
    分钟放量历史明细_<日期>.csv  长表：股票 × 交易日 × 最大分钟量 / 出现时间 / 超阈值分钟数 / 当日总量

依赖：pip install requests pandas
"""
import argparse
import glob
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Referer": "https://finance.sina.com.cn/"}
session = requests.Session()
session.trust_env = False


def sina_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def fetch_m1(code: str, datalen: int = 1500, retry: int = 3):
    """返回 DataFrame[day(YYYY-MM-DD), time(HH:MM), close, vol_hand]"""
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={sina_symbol(code)}&scale=1&ma=no&datalen={datalen}")
    for i in range(retry):
        try:
            data = session.get(url, headers=HEADERS, timeout=20).json()
            if not data:
                raise ValueError("empty")
            df = pd.DataFrame(data)
            df["day"] = df["day"].str[:10]
            df["time"] = pd.to_datetime(pd.DataFrame(data)["day"]).dt.strftime("%H:%M")
            df["close"] = df["close"].astype(float)
            df["vol_hand"] = df["volume"].astype(float) / 100  # 新浪成交量单位为股
            return df[["day", "time", "close", "vol_hand"]]
        except Exception:
            time.sleep(1 + i)
    return None


def find_pool_file() -> str:
    files = sorted(glob.glob(os.path.join(HERE, "分钟放量_*.csv")))
    if not files:
        sys.exit("找不到 分钟放量_*.csv，请先运行 scan_minute_volume.py 或用 -i 指定股票池")
    return files[-1]


def main():
    ap = argparse.ArgumentParser(description="回看近 N 个交易日的分钟级最大成交量")
    ap.add_argument("-i", "--input", help="股票池 CSV（需含「代码」列），默认最新的 分钟放量_*.csv")
    ap.add_argument("-t", "--threshold", type=float, default=50000, help="分钟成交量阈值（手），默认 50000")
    ap.add_argument("-d", "--days", type=int, default=5, help="回看交易日数，默认 5")
    ap.add_argument("--include-today", action="store_true", help="把数据中最新一天也算进回看区间（默认剔除最新一天）")
    ap.add_argument("-w", "--workers", type=int, default=6, help="并发数，默认 6")
    args = ap.parse_args()

    pool_file = args.input or find_pool_file()
    pool = pd.read_csv(pool_file, dtype=str)
    if "代码" not in pool.columns:
        sys.exit(f"{pool_file} 没有「代码」列")
    pool["代码"] = pool["代码"].str.zfill(6)
    names = dict(zip(pool["代码"], pool.get("名称", pool["代码"])))
    print(f"股票池：{pool_file}（{len(pool)} 只），阈值 {args.threshold:.0f} 手，回看 {args.days} 个交易日"
          f"{'（含最新一天）' if args.include_today else '（不含最新一天）'}")

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(fetch_m1, pool["代码"]))

    detail, failed = [], []
    for code, df in zip(pool["代码"], results):
        if df is None or df.empty:
            failed.append(code)
            continue
        days = sorted(df["day"].unique())
        if not args.include_today:
            days = days[:-1]
        days = days[-args.days:]
        for day in days:
            d = df[df["day"] == day]
            top = d.loc[d["vol_hand"].idxmax()]
            over = d[d["vol_hand"] >= args.threshold]
            detail.append({
                "代码": code, "名称": names[code], "交易日": day,
                "最大分钟量(手)": int(top["vol_hand"]), "最大分钟时间": top["time"], "当时价格": top["close"],
                "超阈值分钟数": len(over),
                "首次超阈值时间": over.iloc[0]["time"] if len(over) else "",
                "当日总量(手)": int(d["vol_hand"].sum()),
            })

    if not detail:
        sys.exit("没有取到任何数据")
    det = pd.DataFrame(detail)
    wide = det.pivot(index=["代码", "名称"], columns="交易日", values="最大分钟量(手)")
    wide["超阈值天数"] = (wide >= args.threshold).sum(axis=1)
    wide["回看最大分钟量(手)"] = wide.drop(columns="超阈值天数").max(axis=1).astype(int)
    wide = wide.sort_values(["超阈值天数", "回看最大分钟量(手)"], ascending=False).reset_index()

    stamp = f"{datetime.now():%Y%m%d}"
    wide_path = os.path.join(HERE, f"分钟放量历史_{stamp}.csv")
    det_path = os.path.join(HERE, f"分钟放量历史明细_{stamp}.csv")
    wide.to_csv(wide_path, index=False, encoding="utf-8-sig")
    det.to_csv(det_path, index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print(f"\n每日最大分钟量(手)，>= {args.threshold:.0f} 的算超阈值：")
    print(wide.to_string(index=False))
    if failed:
        print(f"\n{len(failed)} 只未取到数据：{' '.join(failed)}")
    print(f"\n已保存：{wide_path}\n        {det_path}")


if __name__ == "__main__":
    main()
