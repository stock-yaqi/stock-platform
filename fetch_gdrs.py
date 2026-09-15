#!/usr/bin/env python3
"""
拉取全市场「股东户数」历史（东财 F10 股东研究），保存到 mins/_meta/gdrs.json：{代码: [{END_DATE, HOLDER_TOTAL_NUM, ...}, ...]}
已有的按 --refresh-days 判断是否重拉（默认 7 天内不重复）。约 5200 次请求，8 线程 10 分钟左右。
    python3 fetch_gdrs.py            # 增量
    python3 fetch_gdrs.py --force    # 全部重拉
"""
import argparse, glob, json, os, time, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, "mins", "_meta", "gdrs.json")
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/", "Connection": "close"}
KEEP = ["END_DATE", "HOLDER_TOTAL_NUM", "TOTAL_NUM_RATIO", "AVG_MARKET_CAP", "AVG_HOLD_NUM", "HOLD_NOTICE_DATE", "CLOSE_PRICE", "INTERVAL_CHRATE"]
def fetch(code):
    pre = "SH" if code.startswith(("6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
    for i in range(3):
        try:
            j = requests.get(f"https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax?code={pre}{code}", headers=H, proxies={"http": None, "https": None}, timeout=15).json()
            rows = j.get("gdrs") or []
            return code, [{k: r.get(k) for k in KEEP} for r in rows]
        except Exception:
            time.sleep(1 + i)
    return code, None
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--force", action="store_true"); ap.add_argument("--refresh-days", type=int, default=7); a = ap.parse_args()
    data = json.load(open(OUT)) if os.path.exists(OUT) and not a.force else {"fetched": {}, "stocks": {}}
    codes = sorted(os.path.basename(d) for d in glob.glob(os.path.join(HERE, "mins", "[0-9]*")))
    now = datetime.now()
    todo = [c for c in codes if c not in data["stocks"] or (now - datetime.strptime(data["fetched"].get(c, "2000-01-01"), "%Y-%m-%d")).days >= a.refresh_days]
    print(f"待拉 {len(todo)} / {len(codes)}", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for n, (c, rows) in enumerate(ex.map(fetch, todo), 1):
            if rows is not None:
                data["stocks"][c] = rows; data["fetched"][c] = f"{now:%Y-%m-%d}"
            if n % 500 == 0:
                json.dump(data, open(OUT, "w"), ensure_ascii=False); print("进度", n, flush=True)
    json.dump(data, open(OUT, "w"), ensure_ascii=False)
    print("完成，共", len(data["stocks"]), "只；无数据", sum(1 for c in todo if c not in data["stocks"]))
