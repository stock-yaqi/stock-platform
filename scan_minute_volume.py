#!/usr/bin/env python3
"""
扫描股票池中「分钟级成交量超过阈值」的股票。

数据源：腾讯 1 分钟 K 线（免 cookie、不封 IP）。
默认读取同目录下的 国务院国资委控股_破净股_*.csv（取日期最新的一份），
对每只股票拉取最新交易日的全部分钟 K 线，找出任一分钟成交量 >= 阈值（默认 5 万手）的股票。

用法：
    python3 scan_minute_volume.py                       # 默认：最新破净股 CSV，阈值 5 万手
    python3 scan_minute_volume.py -t 30000              # 阈值改为 3 万手
    python3 scan_minute_volume.py -i 其他股票池.csv      # 指定股票池（需有「代码」列）
    python3 scan_minute_volume.py --latest-only         # 只看最新一根分钟 K 线是否超阈值
    python3 scan_minute_volume.py -o result.csv         # 指定输出文件名

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
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

session = requests.Session()
session.trust_env = False  # 不走系统代理，腾讯接口国内直连即可


def tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def fetch_m1(code: str, retry: int = 3):
    """返回该股票最近约 320 根 1 分钟 K 线：[(时间'YYYYMMDDHHMM', 收盘价, 成交量手), ...]"""
    sym = tencent_symbol(code)
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m1,,320"
    for i in range(retry):
        try:
            r = session.get(url, headers=HEADERS, timeout=15)
            bars = r.json()["data"][sym]["m1"]
            return [(b[0], float(b[2]), float(b[5])) for b in bars]
        except Exception:
            time.sleep(1 + i)
    return None


def find_pool_file() -> str:
    files = sorted(glob.glob(os.path.join(HERE, "国务院国资委控股_破净股_*.csv")))
    if not files:
        sys.exit("找不到 国务院国资委控股_破净股_*.csv，请用 -i 指定股票池文件")
    return files[-1]


def main():
    ap = argparse.ArgumentParser(description="扫描分钟级成交量超过阈值的股票")
    ap.add_argument("-i", "--input", help="股票池 CSV（需含「代码」列），默认取最新的破净股文件")
    ap.add_argument("-t", "--threshold", type=float, default=50000, help="分钟成交量阈值（手），默认 50000")
    ap.add_argument("-o", "--output", help="输出 CSV 路径，默认 分钟放量_<日期>.csv")
    ap.add_argument("--latest-only", action="store_true", help="只判断最新一根分钟 K 线，而不是全天任一分钟")
    ap.add_argument("-w", "--workers", type=int, default=8, help="并发数，默认 8")
    args = ap.parse_args()

    pool_file = args.input or find_pool_file()
    pool = pd.read_csv(pool_file, dtype=str)
    if "代码" not in pool.columns:
        sys.exit(f"{pool_file} 没有「代码」列")
    pool["代码"] = pool["代码"].str.zfill(6)
    print(f"股票池：{pool_file}（{len(pool)} 只），阈值 {args.threshold:.0f} 手，"
          f"{'只看最新一分钟' if args.latest_only else '扫描最新交易日全天'}")

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(fetch_m1, pool["代码"]))

    rows, failed = [], []
    for (_, s), bars in zip(pool.iterrows(), results):
        if not bars:
            failed.append(s["代码"])
            continue
        day = bars[-1][0][:8]                       # 数据中最新的交易日
        today = [b for b in bars if b[0].startswith(day)]
        if args.latest_only:
            today = today[-1:]
        hits = [b for b in today if b[2] >= args.threshold]
        if not hits:
            continue
        top = max(today, key=lambda b: b[2])
        rows.append({
            "代码": s["代码"],
            "名称": s.get("名称", ""),
            "交易日": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "超阈值分钟数": len(hits),
            "最大分钟量(手)": int(top[2]),
            "最大分钟时间": f"{top[0][8:10]}:{top[0][10:]}",
            "当时价格": top[1],
            "首次超阈值时间": f"{hits[0][0][8:10]}:{hits[0][0][10:]}",
            "当日总量(手)": int(sum(b[2] for b in today)),
            "现价": today[-1][1] if today else "",
        })

    out = pd.DataFrame(rows).sort_values("最大分钟量(手)", ascending=False) if rows else pd.DataFrame(
        columns=["代码", "名称", "交易日", "超阈值分钟数", "最大分钟量(手)", "最大分钟时间", "当时价格", "首次超阈值时间", "当日总量(手)", "现价"])
    out_path = args.output or os.path.join(HERE, f"分钟放量_{datetime.now():%Y%m%d}.csv")
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"\n命中 {len(out)} 只：")
    if len(out):
        print(out.to_string(index=False))
    if failed:
        print(f"\n{len(failed)} 只未取到数据：{' '.join(failed)}")
    print(f"\n已保存：{out_path}")


if __name__ == "__main__":
    main()
