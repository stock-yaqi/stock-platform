#!/usr/bin/env python3
"""
持仓冲高监控：对邮件里点过「我已买入」的票，从买入次日开始每分钟盯一次，冲高 / 回落 / 无溢价 / 临近 10:00 都发邮件。

规则来自共振打法的卖出端：次日 10:00 前离场，早盘冲高即走。监控只是把「冲高」这一刻抓准，
因为溢价基本集中在 09:30-10:00 这半小时，1 分钟粒度才追得上。

触发条件（每只每天每种只发一次，同一只两次提醒至少间隔 8 分钟；紧急类不受间隔限制）：
    外围大跌  日经或韩国跌 >= 1.5%，09:45 前          → 开盘直接卖，不等冲高
    止损      相对买入价 <= -3%                        → 破位，直接走
    冲高回落  日内最高溢价 >= 1%，现价从日内高回落 >= 0.8% → 冲高已过，立刻卖
    正在拉升  最近 3 分钟涨 >= 1.2% 且创日内新高、有溢价   → 正在冲，挂单卖
    溢价达标  相对买入价 >= 2%                         → 现在卖
    无溢价    09:40 后日内最高仍不高于买入价            → 不会有溢价了，直接卖
    临近截止  09:52 后仍持有                            → 10:00 前必须走，盈亏都走

用法：
    python3 scan_hold.py                 # scheduler 盘中每分钟调用；没持仓 / 非交易时段秒退
    python3 scan_live.py --now           # 忽略时段立刻看一眼
    python3 scan_hold.py --now --no-email
    python3 scan_hold.py --status        # 只打印当前持仓状态，不发信
可调：--target 2 --drop 0.8 --stop 3 --deadline 09:52
"""
import argparse
import fcntl
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402
import positions as P  # noqa: E402

RED, GREEN, GRAY, INK, LINE = L.RED, L.GREEN, L.GRAY, L.INK, L.LINE
# key, 优先级（小的先）, 是否紧急（不受 8 分钟间隔限制）
PRIORITY = ["overseas", "stop", "fade", "surge", "target", "noprem", "deadline"]
URGENT = {"overseas", "stop", "fade", "deadline"}


def log(msg):
    line = f"{datetime.now():%m-%d %H:%M:%S} [hold] {msg}"
    print(line, flush=True)
    os.makedirs(L.LOGS, exist_ok=True)
    with open(os.path.join(L.LOGS, f"hold_{date.today():%Y%m%d}.log"), "a") as f:
        f.write(line + "\n")


def in_window(now):
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "09:25" <= t <= "11:32" or "12:58" <= t <= "15:02"


def judge(pos, q, bars, now_t, args, ov):
    """返回 (key, 结论文字, 颜色, 指标 dict)；没触发返回 (None, ...)"""
    bp = pos.get("buy_price")
    price = q.get("price") or 0
    day_high = q.get("high") or 0
    if not bp or not price:
        return None, "", GRAY, {}
    prem = (price / bp - 1) * 100
    prem_high = (day_high / bp - 1) * 100 if day_high else prem
    fade = (price / day_high - 1) * 100 if day_high else 0.0
    rise3 = new_high = 0.0
    if bars and len(bars) >= 4:
        rise3 = (bars[-1][1] / bars[-4][1] - 1) * 100
        new_high = bars[-1][2] >= max(b[2] for b in bars[:-1])
    m = {"prem": prem, "prem_high": prem_high, "fade": fade, "rise3": rise3, "price": price, "day_high": day_high, "pct": q.get("pct")}

    worst = min([v for k, v in ov.items() if k in ("日经", "韩国")] + [0])
    if worst <= -args.max_overseas_drop and now_t <= "09:45":
        return "overseas", f"外围大跌（{worst:+.2f}%），开盘直接卖，不等冲高", GREEN, m
    if prem <= -args.stop:
        return "stop", f"已跌 {prem:+.2f}%，破位，直接走", GREEN, m
    if prem_high >= 1.0 and fade <= -args.drop:
        return "fade", f"早盘冲到 {prem_high:+.2f}% 已回落 {fade:.2f}%，立刻卖", "#b8742a", m
    if rise3 >= args.surge and new_high and prem > 0:
        return "surge", f"3 分钟拉 {rise3:+.2f}% 创日内新高，溢价 {prem:+.2f}%，正在冲高，挂单卖", RED, m
    if prem >= args.target:
        return "target", f"溢价 {prem:+.2f}% 已到目标，现在卖", RED, m
    if now_t >= "09:40" and prem_high <= 0:
        return "noprem", f"到现在日内最高也没超过买入价（{prem_high:+.2f}%），不会有溢价了，直接卖", GREEN, m
    if now_t >= args.deadline:
        return "deadline", f"快到 10:00 了，按规则必须走，现在 {prem:+.2f}%", GREEN, m
    return None, "", GRAY, m


def main():
    ap = argparse.ArgumentParser(description="持仓冲高监控")
    ap.add_argument("--now", action="store_true", help="忽略交易时段立刻跑一遍")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--status", action="store_true", help="只打印状态")
    ap.add_argument("--target", type=float, default=2.0, help="溢价达标线 %%")
    ap.add_argument("--drop", type=float, default=0.8, help="从日内高回落多少 %% 算冲高结束")
    ap.add_argument("--stop", type=float, default=3.0, help="止损线 %%")
    ap.add_argument("--surge", type=float, default=1.2, help="最近 3 分钟涨幅达到多少 %% 算正在拉升")
    ap.add_argument("--deadline", default="09:52", help="超过这个时间提醒必须离场")
    ap.add_argument("--max-overseas-drop", type=float, default=1.5)
    ap.add_argument("--include-today", action="store_true", help="当天买的也监控（默认只监控买入次日起）")
    args = ap.parse_args()

    now = datetime.now()
    today = f"{now:%Y%m%d}"
    if not args.now and not args.status and not in_window(now):
        return
    d = P.load()
    rows = [p for p in P.holding(d) if args.include_today or args.status or p["buy_date"] < today]
    if not rows:
        return
    os.makedirs(L.META, exist_ok=True)
    fh = open(os.path.join(L.META, "hold.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return

    codes = [p["code"] for p in rows]
    q = L.batch_quotes(codes)
    with ThreadPoolExecutor(4) as ex:
        bars_map = dict(zip(codes, ex.map(lambda c: L.fetch_m1_today(c, today), codes)))
    ov = L.overseas() if not args.status else {}
    now_t = now.strftime("%H:%M")

    hits, board = [], []
    for p in rows:
        s = q.get(p["code"], {})
        key, text, col, m = judge(p, s, bars_map.get(p["code"]), now_t, args, ov)
        board.append((p, m, key, text))
        if args.status or not key:
            continue
        a = p.setdefault("alerts", {}).setdefault(today, {"fired": [], "last": ""})
        if key in a["fired"]:
            continue
        if key not in URGENT and a["last"] and L.minutes_between(a["last"], now_t) < 8:
            log(f"{p['code']} {p['name']} 触发 {key} 但距上次提醒不足 8 分钟，压后")
            continue
        a["fired"].append(key)
        a["last"] = now_t
        hits.append((p, m, key, text, col))

    for p, m, key, text in board:
        log(f"{p['code']} {p['name']} 买 {p.get('buy_price')} 现 {m.get('price')} 溢价 {m.get('prem', 0):+.2f}% "
            f"日内高溢价 {m.get('prem_high', 0):+.2f}% 回落 {m.get('fade', 0):+.2f}% 3分钟 {m.get('rise3', 0):+.2f}% → {key or '无动作'}")
    if args.status:
        return
    if not hits:
        return
    if not args.no_email:
        P.save(d)   # 只有真发信才记「已提醒」，--no-email 演练不能把该发的邮件压掉

    ov_line = "  ".join(f"{k} {v:+.2f}%" for k, v in ov.items()) or "外围数据缺失"
    head = hits[0][3] if len(hits) == 1 else f"{len(hits)} 只触发卖出提醒"
    lines = [f"{now:%Y-%m-%d %H:%M}  持仓冲高监控", f"外围：{ov_line}", ""]
    parts = []
    for p, m, key, text, col in hits:
        lines.append(f"  {p['code']} {p['name']}  买入 {p['buy_price']}（{p['buy_date'][4:6]}-{p['buy_date'][6:]} {p['buy_time']}）"
                     f"  现价 {m['price']}  溢价 {m['prem']:+.2f}%  日内最高溢价 {m['prem_high']:+.2f}%  → {text}")
        parts.append(f"<div style='margin-top:12px;padding:12px;border-left:4px solid {col};background:#f7f7f4'>"
                     f"<div style='font-size:16px;font-weight:700'>{L.h_name(p['name'], p['code'])}</div>"
                     f"<div style='font-size:17px;font-weight:700;color:{col};margin-top:6px'>{text}</div></div>")
        parts.append(f"<div style='padding:8px 0 2px;font-size:13px;line-height:1.7'>"
                     + L.h_kv(("买入价", f"{p['buy_price']:g}"), ("现价", f"{m['price']:g}"), ("溢价", L.h_pct(m["prem"])),
                              ("买入时间", f"{p['buy_date'][4:6]}-{p['buy_date'][6:]} {p['buy_time']}"))
                     + "<br>"
                     + L.h_kv(("日内最高", f"{m['day_high']:g}"), ("最高溢价", L.h_pct(m["prem_high"])),
                              ("距日内高", L.h_pct(m["fade"])), ("今日", L.h_pct(m["pct"]) if m.get("pct") is not None else "-"))
                     + "</div>")
        base = L.web_base()
        if base:
            tok = L.load_env().get("WEB_TOKEN", "")
            parts.append(f"<div style='padding:4px 0 14px'><a href='{base}/sell?c={p['code']}&k={tok}' "
                         f"style='display:inline-block;padding:9px 18px;border-radius:6px;background:{GREEN};color:#fff;"
                         f"text-decoration:none;font-size:14px;font-weight:600'>✓ 我已卖出（停止监控）</a></div>")
    lines += ["", "规则：次日 10:00 前离场，早盘冲高即走；溢价集中在开盘后 30 分钟，10:00 之后只会更差。亏损单一样走，不等回本。",
              "卖掉后请点邮件里的「我已卖出」，否则明天还会继续提醒。"]
    html = L.h_wrap(f"持仓冲高监控 · {now:%H:%M}", [f"{now:%Y-%m-%d} · 外围 {ov_line}"], parts,
                    "次日 10:00 前离场，早盘冲高即走；溢价集中在开盘后 30 分钟。卖掉后点「我已卖出」，否则明天还会提醒。仅提醒，非建议。")
    body = "\n".join(lines)
    print(body)
    log(f"发提醒 {len(hits)} 只：" + " ".join(f"{p['code']}{p['name']}({key})" for p, _, key, _, _ in hits))
    if not args.no_email:
        L.send_mail(f"【冲高提醒】{now:%H:%M} " + head, body, html)


if __name__ == "__main__":
    main()
