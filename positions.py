#!/usr/bin/env python3
"""
持仓台账：邮件里点「我已买入」后写在这里，次日由 scan_hold.py 做 1 分钟冲高监控。

存储：mins/_meta/positions.json  {"positions": [ {...}, ... ]}
字段：code name buy_price buy_date buy_time sector signal_price source status(holding/sold) sold_* alerts{} note
命令行：
    python3 positions.py                          # 看当前持仓
    python3 positions.py --all                    # 含已卖出
    python3 positions.py --add 300123 --price 12.3   # 手工加一笔（不点邮件也能加）
    python3 positions.py --sell 300123
    python3 positions.py --remove 300123
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402

PATH = os.path.join(L.META, "positions.json")


def load():
    if not os.path.exists(PATH):
        return {"positions": []}
    try:
        return json.load(open(PATH))
    except Exception:
        return {"positions": []}


def save(d):
    os.makedirs(L.META, exist_ok=True)
    tmp = PATH + ".tmp"
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def holding(d=None):
    """当前还拿着的仓位"""
    return [p for p in (d or load())["positions"] if p.get("status") == "holding"]


def find(code, d=None, only_holding=True):
    for p in reversed((d or load())["positions"]):
        if p["code"] == code and (not only_holding or p.get("status") == "holding"):
            return p
    return None


def add(code, name=None, price=None, sector="", signal_price=None, source="邮件点击", note=""):
    """记一笔买入；同一只已持有则只更新价格，不重复建仓。返回 (position, 是否新建)"""
    code = str(code).zfill(6)
    d = load()
    if price is None or not name:
        q = L.batch_quotes([code]).get(code, {})
        price = price or q.get("price")
        name = name or q.get("name") or code
    now = datetime.now()
    old = find(code, d)
    if old:
        if price:
            old["buy_price"] = round(float(price), 3)
        old["note"] = note or old.get("note", "")
        save(d)
        return old, False
    p = {"code": code, "name": name, "buy_price": round(float(price), 3) if price else None,
         "buy_date": f"{now:%Y%m%d}", "buy_time": f"{now:%H:%M}", "sector": sector,
         "signal_price": signal_price, "source": source, "status": "holding",
         "sold_date": None, "sold_time": None, "sold_price": None, "alerts": {}, "note": note}
    d["positions"].append(p)
    save(d)
    return p, True


def sell(code, price=None):
    code = str(code).zfill(6)
    d = load()
    p = find(code, d)
    if not p:
        return None
    if price is None:
        price = L.batch_quotes([code]).get(code, {}).get("price")
    now = datetime.now()
    p["status"] = "sold"
    p["sold_date"], p["sold_time"] = f"{now:%Y%m%d}", f"{now:%H:%M}"
    p["sold_price"] = round(float(price), 3) if price else None
    if p["sold_price"] and p.get("buy_price"):
        p["pnl"] = round((p["sold_price"] / p["buy_price"] - 1) * 100, 2)
    save(d)
    return p


def remove(code):
    d = load()
    n = len(d["positions"])
    d["positions"] = [p for p in d["positions"] if not (p["code"] == str(code).zfill(6) and p.get("status") == "holding")]
    save(d)
    return n - len(d["positions"])


def set_price(code, price):
    d = load()
    p = find(code, d)
    if p:
        p["buy_price"] = round(float(price), 3)
        save(d)
    return p


def main():
    ap = argparse.ArgumentParser(description="持仓台账")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--add")
    ap.add_argument("--sell")
    ap.add_argument("--remove")
    ap.add_argument("--price", type=float)
    a = ap.parse_args()
    if a.add:
        p, isnew = add(a.add, price=a.price, source="命令行")
        print(("已记录买入 " if isnew else "已更新 ") + f"{p['code']} {p['name']} @ {p['buy_price']}")
        return
    if a.sell:
        p = sell(a.sell, a.price)
        print(f"已标记卖出 {p['code']} {p['name']} @ {p['sold_price']}  盈亏 {p.get('pnl')}%" if p else "没有这只持仓")
        return
    if a.remove:
        print(f"已删除 {remove(a.remove)} 条")
        return
    d = load()
    rows = d["positions"] if a.all else holding(d)
    if not rows:
        print("当前没有持仓")
        return
    q = L.batch_quotes([p["code"] for p in rows])
    print(f"{'代码':<8}{'名称':<8}{'买入日':<10}{'买入价':>8}{'现价':>8}{'盈亏%':>8}  状态")
    for p in rows:
        now_p = q.get(p["code"], {}).get("price")
        pnl = (now_p / p["buy_price"] - 1) * 100 if now_p and p.get("buy_price") else None
        st = p["status"] if p["status"] == "holding" else f"已卖 {p['sold_date']} {p.get('pnl', '')}%"
        print(f"{p['code']:<8}{p['name']:<8}{p['buy_date']:<10}{p['buy_price'] or 0:>8.2f}{now_p or 0:>8.2f}{pnl if pnl is not None else 0:>8.2f}  {st}")


if __name__ == "__main__":
    main()
