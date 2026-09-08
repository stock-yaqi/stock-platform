#!/usr/bin/env python3
"""
A 股全市场 1 分钟 K 线本地库。

目录结构：
    mins/<股票代码>/<YYYYMMDD>.csv      每只股票一个目录，每个交易日一个文件
    mins/_meta/stocklist.json           股票列表缓存（新浪，每天刷新）
    mins/_logs/<YYYYMMDD>.log           运行日志

CSV 列：time,open,high,low,close,volume_hand,amount
    time         HH:MM，通达信口径，09:31 为第一根，15:00 为最后一根，整日 240 根
    volume_hand  成交量，单位手（1 手 = 100 股）
    amount       成交额，单位元

数据源：
    主源  通达信行情服务器（pytdx），1 分钟 K 线可回溯约 4 个多月，不需要 cookie
    兜底  新浪 1 分钟 K 线（最多约 6 个交易日），用于北交所股票和通达信失败的股票

用法：
    python3 mins_sync.py                    # 每日模式：补最近 3 个交易日，已有的完整日期不重复拉；非交易日自动跳过
    python3 mins_sync.py --days 65 --full   # 回填模式：拉最近 65 个交易日（约 3 个月），全部重写
    python3 mins_sync.py --codes 601868 000001   # 只拉指定股票
    python3 mins_sync.py -w 8               # 并发数（每个并发一条通达信连接）

依赖：pip install pytdx requests pandas
"""
import argparse
import glob
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "mins")
META = os.path.join(ROOT, "_meta")
LOGS = os.path.join(ROOT, "_logs")
HEADER = "time,open,high,low,close,volume_hand,amount\n"
FULL_DAY_BARS = 240

# 实测可用的通达信服务器，脚本启动时会逐个探测，只用能返回数据的
TDX_HOSTS = [
    ("60.12.136.250", 7709), ("115.238.90.165", 7709), ("180.153.18.170", 7709),
    ("119.147.212.81", 7709), ("123.125.108.14", 7709), ("114.80.63.12", 7709),
    ("218.108.98.244", 7709), ("111.230.186.91", 7709), ("119.29.51.30", 7709),
    ("47.103.48.45", 7709), ("124.71.187.122", 7709), ("120.79.60.82", 7709),
    ("175.6.5.153", 7709), ("122.224.90.24", 7709), ("221.231.141.60", 7709),
    ("101.227.73.20", 7709), ("14.215.128.18", 7709), ("59.173.18.140", 7709),
]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Referer": "https://finance.sina.com.cn/"}
http = requests.Session()
http.trust_env = False  # 国内接口直连，不走系统代理

_log_lock = threading.Lock()
_log_file = None


def log(msg: str):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


# ---------------------------------------------------------------- 股票列表
def load_stock_list(force_refresh=False):
    os.makedirs(META, exist_ok=True)
    cache = os.path.join(META, "stocklist.json")
    today = f"{date.today():%Y%m%d}"
    if not force_refresh and os.path.exists(cache):
        try:
            c = json.load(open(cache))
            if c.get("date") == today and c.get("stocks"):
                return c["stocks"]
        except Exception:
            pass
    stocks = []
    try:
        for page in range(1, 100):
            url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a")
            data = http.get(url, headers=UA, timeout=20).json()
            if not data:
                break
            stocks += [{"code": x["symbol"][2:], "name": x["name"], "ex": x["symbol"][:2]} for x in data]
        if len(stocks) > 4000:
            json.dump({"date": today, "stocks": stocks}, open(cache, "w"), ensure_ascii=False)
            return stocks
    except Exception as e:
        log(f"新浪股票列表获取失败：{e!r}")
    if os.path.exists(cache):  # 用旧缓存
        c = json.load(open(cache))
        log(f"使用 {c.get('date')} 的股票列表缓存")
        return c["stocks"]
    sys.exit("无法获取股票列表")


# ---------------------------------------------------------------- 通达信
def probe_tdx_hosts():
    from pytdx.hq import TdxHq_API
    good = []
    for h, p in TDX_HOSTS:
        api = TdxHq_API(heartbeat=False)
        try:
            if api.connect(h, p, time_out=3) and api.get_security_bars(8, 1, "000001", 0, 1):
                good.append((h, p))
            api.disconnect()
            if len(good) >= 3:
                break
        except Exception:
            pass
    return good


_tls = threading.local()
_hosts = []
_host_idx = [0]
_host_lock = threading.Lock()


def _tdx_api():
    """每个线程一条连接，失败时换下一台服务器重连"""
    from pytdx.hq import TdxHq_API
    api = getattr(_tls, "api", None)
    if api is not None:
        return api
    for _ in range(len(_hosts) * 2):
        with _host_lock:
            h, p = _hosts[_host_idx[0] % len(_hosts)]
            _host_idx[0] += 1
        api = TdxHq_API(heartbeat=False)
        try:
            if api.connect(h, p, time_out=5):
                _tls.api = api
                return api
        except Exception:
            pass
    raise RuntimeError("通达信服务器全部连接失败")


def _tdx_reset():
    api = getattr(_tls, "api", None)
    if api is not None:
        try:
            api.disconnect()
        except Exception:
            pass
    _tls.api = None


def fetch_tdx(code: str, market: int, want_days: int, stop_dates: set):
    """
    从最新往回翻页拉 1 分钟 K 线，按日期分组返回 {YYYYMMDD: [rows]}。
    翻到「已完整存在于本地」的日期（stop_dates）或凑够 want_days 个交易日即停。
    """
    days = defaultdict(list)
    for attempt in range(3):
        try:
            api = _tdx_api()
            days.clear()
            start = 0
            done = False
            while not done and start < 800 * 120:
                bars = api.get_security_bars(8, market, code, start, 800)
                if not bars:
                    break
                for b in reversed(bars):  # 新 -> 旧
                    d = b["datetime"][:10].replace("-", "")
                    if d not in days:
                        if d in stop_dates or len(days) >= want_days:
                            done = True
                            break
                    days[d].append((b["datetime"][11:16], b["open"], b["high"], b["low"], b["close"],
                                    b["vol"] / 100.0, b["amount"]))
                if len(bars) < 800:
                    break
                start += 800
            return {d: sorted(rows) for d, rows in days.items()}
        except Exception as e:
            _tdx_reset()
            time.sleep(1 + attempt)
    return None


# ---------------------------------------------------------------- 新浪兜底
def fetch_sina(code: str, ex: str, want_days: int, stop_dates: set):
    """新浪 1 分钟 K 线，一次最多 1500 根（约 6 个交易日），成交量单位股"""
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={ex}{code}&scale=1&ma=no&datalen=1500")
    for attempt in range(3):
        try:
            bars = http.get(url, headers=UA, timeout=20).json()
            if not bars:
                return {}
            days = defaultdict(list)
            for b in reversed(bars):
                d = b["day"][:10].replace("-", "")
                if d not in days and (d in stop_dates or len(days) >= want_days):
                    break
                days[d].append((b["day"][11:16], float(b["open"]), float(b["high"]), float(b["low"]),
                                float(b["close"]), float(b["volume"]) / 100.0, float(b["amount"])))
            return {d: sorted(rows) for d, rows in days.items()}
        except Exception:
            time.sleep(1 + attempt)
    return None


# ---------------------------------------------------------------- 本地文件
def complete_dates(code: str) -> set:
    """本地已有且行数达到整日的日期（不含最新一天，最新一天总是重拉以防盘中数据）"""
    d = os.path.join(ROOT, code)
    if not os.path.isdir(d):
        return set()
    files = sorted(glob.glob(os.path.join(d, "*.csv")))
    ok = set()
    for f in files[:-1]:
        try:
            with open(f, "rb") as fh:
                n = sum(1 for _ in fh) - 1
            if n >= FULL_DAY_BARS:
                ok.add(os.path.basename(f)[:8])
        except Exception:
            pass
    return ok


def write_days(code: str, days: dict) -> int:
    d = os.path.join(ROOT, code)
    os.makedirs(d, exist_ok=True)
    n = 0
    for day, rows in days.items():
        tmp = os.path.join(d, f"{day}.csv.tmp")
        with open(tmp, "w") as f:
            f.write(HEADER)
            for t, o, h, l, c, v, a in rows:
                f.write(f"{t},{o:g},{h:g},{l:g},{c:g},{v:.0f},{a:.0f}\n")
        os.replace(tmp, os.path.join(d, f"{day}.csv"))
        n += 1
    return n


# ---------------------------------------------------------------- 主流程
def sync_one(stock: dict, want_days: int, full: bool):
    code, ex = stock["code"], stock["ex"]
    stop = set() if full else complete_dates(code)
    days = None
    if ex in ("sh", "sz"):
        days = fetch_tdx(code, 1 if ex == "sh" else 0, want_days, stop)
    if not days:
        days = fetch_sina(code, ex, want_days, stop)
    if days is None:
        return code, None
    return code, write_days(code, days)


def latest_trading_day() -> str:
    """用上证指数/平安银行的最新分钟 K 线判断最近一个交易日"""
    try:
        api = _tdx_api()
        b = api.get_security_bars(8, 0, "000001", 0, 1)
        return b[-1]["datetime"][:10].replace("-", "")
    except Exception:
        return ""


def main():
    global _log_file, _hosts
    ap = argparse.ArgumentParser(description="A 股全市场 1 分钟 K 线本地同步")
    ap.add_argument("--days", type=int, default=3, help="回看交易日数，默认 3（每日模式）；回填 3 个月用 65")
    ap.add_argument("--full", action="store_true", help="忽略本地已有文件，全部重写（回填模式）")
    ap.add_argument("--codes", nargs="*", help="只处理这些代码")
    ap.add_argument("-w", "--workers", type=int, default=6, help="并发数，默认 6")
    ap.add_argument("--force", action="store_true", help="非交易日也强制执行")
    args = ap.parse_args()

    os.makedirs(LOGS, exist_ok=True)
    _log_file = open(os.path.join(LOGS, f"{date.today():%Y%m%d}.log"), "a")
    t0 = time.time()
    log(f"===== 开始 days={args.days} full={args.full} workers={args.workers}")

    _hosts = probe_tdx_hosts()
    log(f"可用通达信服务器 {len(_hosts)} 台：{' '.join(h for h, _ in _hosts)}")
    if not _hosts:
        log("没有可用的通达信服务器，全部走新浪兜底（最多约 6 个交易日）")

    if _hosts and not args.full and not args.force:
        ltd = latest_trading_day()
        today = f"{date.today():%Y%m%d}"
        if ltd and ltd != today:
            log(f"今天 {today} 不是交易日或数据尚未更新（最新交易日 {ltd}），跳过。加 --force 可强制执行")
            return
    _tdx_reset()

    stocks = load_stock_list()
    if args.codes:
        want = {c.zfill(6) for c in args.codes}
        stocks = [s for s in stocks if s["code"] in want]
    log(f"股票 {len(stocks)} 只")

    written, failed, done = 0, [], 0
    with ThreadPoolExecutor(args.workers) as ex:
        for code, n in ex.map(lambda s: sync_one(s, args.days, args.full), stocks):
            done += 1
            if n is None:
                failed.append(code)
            else:
                written += n
            if done % 500 == 0:
                log(f"进度 {done}/{len(stocks)}，已写 {written} 个文件，失败 {len(failed)}")

    log(f"===== 完成：{len(stocks)} 只，写入 {written} 个日文件，失败 {len(failed)} 只，耗时 {time.time() - t0:.0f}s")
    if failed:
        log("失败代码：" + " ".join(failed))


if __name__ == "__main__":
    main()
