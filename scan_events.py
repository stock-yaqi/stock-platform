#!/usr/bin/env python3
"""
事件日历：美股 AI 硬件链催化剂（英伟达 / 美光 / 博通 / 台积电月营收 ...）前后的两封邮件。

    埋伏邮件（事件日 14:00 北京时间）
        从 chains.json 对应链条里挑「前期龙头 + MACD 回到零轴附近 + 当天砸盘被接住」的股票，按满足条件数排序。
        逻辑来自案例：科翔股份 8/26（英伟达财报前一天）买入，8/27 板块共振 +14%。
    反应邮件（次日 08:00 北京时间）
        取雅虎盘后 5 分钟线，算出财报后盘后涨跌幅，给出处理建议：
        盘后 >= +3%  按共振日处理，早盘冲高卖；-2% ~ +3% 观望，10:00 前离场；<= -2% 开盘直接走。
        台积电月营收这类盘中事件：早上只发提醒，盘中靠 scan_live.py 抓共振。

用法：
    python3 scan_events.py --auto            # launchd 调用：按当前时间自动选 pre（>=12 点）或 post（<12 点）
    python3 scan_events.py --pre  [--date 2026-09-30] [--no-email]   # 手动跑埋伏名单
    python3 scan_events.py --post [--date 2026-09-30] [--no-email]   # 手动跑反应
    python3 scan_events.py --list            # 列出未来事件
依赖：scan_live.py 同目录（复用邮件 / 行情 / 缓存），.env 邮件配置，代理 127.0.0.1:10809（雅虎）
"""
import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402

PROXY = {"http": "http://127.0.0.1:10809", "https": "http://127.0.0.1:10809"}
ROOT = L.ROOT


def load_events():
    ev = json.load(open(os.path.join(HERE, "events.json")))["events"]
    return sorted(ev, key=lambda e: e["date"])


def load_chains():
    return json.load(open(os.path.join(HERE, "chains.json")))["chains"]


def prev_trading_days(n=70):
    """本地 mins 里 000001 的最近 n 个交易日"""
    import glob
    fs = sorted(glob.glob(os.path.join(ROOT, "000001", "*.csv")))
    return [os.path.basename(f)[:8] for f in fs[-n:]]


def daily_closes(code, days):
    out = []
    for d in days:
        f = os.path.join(ROOT, code, f"{d}.csv")
        if os.path.exists(f):
            try:
                x = pd.read_csv(f, usecols=["close"])
                if len(x) >= 100:
                    out.append(float(x["close"].iloc[-1]))
            except Exception:
                pass
    return out


def macd_dif(closes):
    s = pd.Series(closes)
    dif = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    return dif.iloc[-1], dif.iloc[-2] if len(dif) > 1 else np.nan


def chain_codes(chains, names, industry):
    codes = set()
    for n in names:
        ch = chains.get(n, {})
        codes.update(ch.get("codes", []))
        for kw in ch.get("keywords", []):
            codes.update(c for c, v in industry.items() if kw in (v.get("em") or ""))
    return sorted(c for c in codes if not c.startswith("92"))


# ---------------------------------------------------------------- 埋伏名单
def pre_scan(ev_list, args):
    industry = json.load(open(os.path.join(L.META, "industry_em.json")))
    chains = load_chains()
    cache = L.load_cache()
    days = prev_trading_days(70)
    lines_all = []
    parts = []
    for ev in ev_list:
        codes = [c for c in chain_codes(chains, ev["chains"], industry) if cache.get(c, {}).get("avg20", 0) >= args.min_amt * 1e8]
        q = L.batch_quotes(codes)
        rows = []
        for c in codes:
            s = q.get(c)
            if not s or s["price"] <= 0:
                continue
            cl = daily_closes(c, days)
            if len(cl) < 35:
                continue
            prev_close = s.get("prev_close") or cl[-1]
            hist = cl[:-1] if s["time"][:8] == days[-1] else cl
            dif, dif_prev = macd_dif(hist + [s["price"]])
            high60 = cache[c]["high60"]
            dd60 = (s["price"] / high60 - 1) * 100
            low = s.get("low") or s["price"]
            dip = (low / prev_close - 1) * 100
            rebound = (s["price"] / low - 1) * 100 if low > 0 else 0
            conds = {
                "前期龙头": dd60 <= -25,
                "MACD零轴附近": abs(dif) / s["price"] <= 0.01,
                "DIF上行": dif > dif_prev,
                "砸盘被接": dip <= -2 and rebound >= 2,
                "未先涨": s["pct"] < 3,
            }
            rows.append({"code": c, "name": s["name"], "price": s["price"], "pct": s["pct"], "dd60": dd60,
                         "dif_pct": dif / s["price"] * 100, "dip": dip, "rebound": rebound,
                         "avg20": cache[c]["avg20"] / 1e8, "amt_ratio": s["amount"] / cache[c]["avg20"],
                         "score": sum(conds.values()), "tags": " ".join(k for k, v in conds.items() if v)})
        if not rows:
            lines_all.append(f"■ {ev['date']} {ev['name']}：链条 {'/'.join(ev['chains'])} 无可用候选")
            parts.append(L.h_section(ev["name"], "无可用候选"))
            continue
        df = pd.DataFrame(rows).sort_values(["score", "avg20"], ascending=[False, False])
        head = (f"■ {ev['date']} {ev['name']}（{ev['ticker']}，{'美股盘后' if ev['type'] == 'after_close' else '北京时间盘中'}"
                f"{'' if ev.get('confirmed') else '，日期为估计值'}）  链条：{'/'.join(ev['chains'])}  候选 {len(df)} 只")
        lines = [head, "  条件：前期龙头(距60日高<=-25%) / MACD零轴附近(|DIF|<=1%价) / DIF上行 / 砸盘被接(日内低点<=-2%且反弹>=2%) / 未先涨(<3%)"]
        for r in df.head(args.top).itertuples():
            lines.append(f"  [{r.score}/5] {r.code} {r.name:<6} 现价 {r.price:<8} 涨 {r.pct:+.2f}%  距60日高 {r.dd60:+.0f}%  DIF {r.dif_pct:+.2f}%  "
                         f"低点 {r.dip:+.1f}% 反弹 {r.rebound:+.1f}%  20日均额 {r.avg20:.1f}亿 今日量比 {r.amt_ratio:.2f}  {r.tags}")
        lines_all.append("\n".join(lines))
        parts.append(L.h_section(f"{ev['name']} <span style='color:{L.GRAY};font-weight:400;font-size:12px'>{ev['ticker']}</span>",
                                 f"{ev['date']} {'美股盘后' if ev['type'] == 'after_close' else '北京时间盘中'}{'' if ev.get('confirmed') else ' · 日期为估计'} · {'/'.join(ev['chains'])} · 候选 {len(df)} 只"))
        for r in df.head(args.top).itertuples():
            tags = " ".join(f"<span style='padding:1px 5px;border-radius:3px;background:#f0f0ec;color:{L.INK};font-size:11px'>{t}</span>" for t in r.tags.split())
            parts.append(L.h_card(
                f"{r.name} <span style='color:{L.GRAY};font-weight:400;font-size:12px'>{r.code}</span>", f"<b>{r.score}</b><span style='color:{L.GRAY};font-size:12px'>/5</span>",
                L.h_kv(("现价", f"{r.price:g}"), ("今日", L.h_pct(r.pct)), ("距60日高", f"{r.dd60:+.0f}%"), ("DIF", f"{r.dif_pct:+.2f}%")),
                L.h_kv(("低点", f"{r.dip:+.1f}%"), ("反弹", f"{r.rebound:+.1f}%"), ("20日均额", f"{r.avg20:.1f}亿"), ("量比", f"{r.amt_ratio:.2f}")) + "<br>" + tags))
        df.to_csv(os.path.join(HERE, f"事件埋伏_{ev['date']}_{ev['ticker']}.csv"), index=False, encoding="utf-8-sig")
    body = "\n\n".join(lines_all) + ("\n\n事件出结果后次日 08:00 会再发一封盘后反应邮件。规则：次日 10:00 前离场，盘后跌超 2% 开盘直接走。"
                                     "\n这是筛选名单，不是买入建议。")
    html = L.h_wrap("事件埋伏名单", ["条件：前期龙头（距60日高 ≤ -25%）· MACD 零轴附近（|DIF| ≤ 1% 价）· DIF 上行 · 砸盘被接（日内低点 ≤ -2% 且反弹 ≥ 2%）· 未先涨（< 3%）",
                                   "得分越高越接近案例形态（科翔 8/26）"], parts,
                    "事件出结果后次日 07:40 会再发一封盘后反应邮件。规则：次日 10:00 前离场，盘后跌超 2% 开盘直接走。这是筛选名单，不是买入建议。")
    print(body)
    if not args.no_email:
        L.send_mail(f"【事件埋伏】{'、'.join(e['name'] for e in ev_list)}", body, html)


# ---------------------------------------------------------------- 盘后反应
def yahoo_reaction(ticker):
    """返回 (正常时段涨跌%, 盘后涨跌%, 盘后最新价, 正常收盘价)"""
    j = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2d&interval=5m&includePrePost=true",
                     headers={"User-Agent": "Mozilla/5.0"}, proxies=PROXY, timeout=30).json()["chart"]["result"][0]
    m = j["meta"]
    reg_close = m["regularMarketPrice"]
    reg_pct = m.get("regularMarketChangePercent")
    if reg_pct is None and m.get("chartPreviousClose"):
        reg_pct = (reg_close / m["chartPreviousClose"] - 1) * 100
    post = m.get("currentTradingPeriod", {}).get("post", {})
    ts = j["timestamp"]
    cl = j["indicators"]["quote"][0]["close"]
    post_bars = [(t, c) for t, c in zip(ts, cl) if c and post and t >= post.get("start", 0) - 1]
    # 正常时段结束时间：用 regularMarketTime
    reg_t = m.get("regularMarketTime", 0)
    post_bars = [(t, c) for t, c in post_bars if t > reg_t]
    if post_bars:
        last = post_bars[-1][1]
        return reg_pct, (last / reg_close - 1) * 100, last, reg_close
    return reg_pct, None, None, reg_close


def post_scan(ev_list, args):
    lines = []
    parts = []
    for ev in ev_list:
        if ev["type"] != "after_close":
            lines.append(f"■ {ev['name']}（{ev['ticker']}）今天北京时间盘中出结果，盘中留意 {'/'.join(ev['chains'])} 板块是否共振，scan_live 会自动抓。")
            parts.append(L.h_section(ev["name"], "北京时间盘中出结果") + f"<div style='padding:10px 0'>盘中留意 <b>{'/'.join(ev['chains'])}</b> 板块是否共振，scan_live 会自动抓。</div>")
            continue
        try:
            reg, post, last, close = yahoo_reaction(ev["ticker"])
        except Exception as e:
            lines.append(f"■ {ev['name']}（{ev['ticker']}）盘后数据获取失败：{e!r}，请手动看一眼再决定。")
            parts.append(L.h_section(ev["name"], ev["ticker"]) + "<div style='padding:10px 0'>盘后数据获取失败，请手动看一眼再决定。</div>")
            continue
        if post is None:
            lines.append(f"■ {ev['name']}（{ev['ticker']}）正常时段 {reg:+.2f}%，盘后暂无成交数据，请手动确认。")
            parts.append(L.h_section(ev["name"], ev["ticker"]) + f"<div style='padding:10px 0'>正常时段 {L.h_pct(reg)}，盘后暂无成交数据，请手动确认。</div>")
            continue
        if post >= 3:
            advice = "盘后大涨 → 按共振日处理：持有到早盘冲高卖，10:00 前离场；未持仓者盘中看 scan_live 的共振信号。"
        elif post <= -2:
            advice = "盘后下跌 → 开盘直接卖出，不等冲高。"
        else:
            advice = "盘后反应平淡 → 观望，早盘有溢价就走，最晚 10:00 前离场。"
        lines.append(f"■ {ev['name']}（{ev['ticker']}）正常时段 {reg:+.2f}%，盘后 {post:+.2f}%（{close:.2f} → {last:.2f}）\n  {advice}")
        color = L.RED if post >= 3 else L.GREEN if post <= -2 else "#b8742a"
        parts.append(L.h_section(ev["name"], ev["ticker"]) +
                     f"<div style='padding:12px 0;border-bottom:1px solid {L.LINE}'><div style='font-size:26px;font-weight:700;color:{color}'>盘后 {post:+.2f}%</div>"
                     f"<div style='color:{L.GRAY};font-size:12px;margin-top:2px'>正常时段 {L.h_pct(reg)} · {close:.2f} → {last:.2f}</div>"
                     f"<div style='margin-top:8px;line-height:1.6'>{advice}</div></div>")
    ov = L.overseas()
    ov_line = "  ".join(f"{k} {v:+.2f}%" for k, v in ov.items())
    lines.append("外围：" + ov_line)
    body = "\n\n".join(lines)
    html = L.h_wrap("事件盘后反应", ["外围 " + " · ".join(f"{k} {L.h_pct(v)}" for k, v in ov.items())], parts,
                    "盘后 ≥ +3% 按共振日处理，早盘冲高卖；-2% ~ +3% 观望，10:00 前离场；≤ -2% 开盘直接走。")
    print(body)
    if not args.no_email:
        L.send_mail(f"【事件反应】{'、'.join(e['name'] for e in ev_list)}", body, html)


def main():
    ap = argparse.ArgumentParser(description="AI 链事件日历：埋伏名单 / 盘后反应")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--pre", action="store_true")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--date", help="指定事件日期（美东），默认今天(pre)/昨天(post)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-amt", type=float, default=3.0, help="20 日均成交额下限（亿）")
    args = ap.parse_args()
    events = load_events()
    if args.list:
        today = f"{date.today():%Y-%m-%d}"
        for e in events:
            if e["date"] >= today:
                print(f"{e['date']}  {e['name']:<14} {e['ticker']:<5} {'已确认' if e.get('confirmed') else '估计'}  链条 {'/'.join(e['chains'])}  {e.get('note', '')}")
        return
    now = datetime.now()
    mode = "pre" if args.pre else "post" if args.post else ("post" if now.hour < 12 else "pre") if args.auto else None
    if not mode:
        ap.print_help()
        return
    if mode == "pre":
        d = args.date or f"{now:%Y-%m-%d}"
        # 盘中事件（台积电）前一天 14:00 也发埋伏：把 date 为明天的 intraday 事件一起算
        tomorrow = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        ev = [e for e in events if (e["date"] == d and e["type"] == "after_close") or (e["date"] == tomorrow and e["type"] == "intraday")]
        if not ev:
            L.log(f"{d} 无事件，跳过埋伏扫描")
            return
        L.log(f"埋伏扫描：{'、'.join(e['name'] for e in ev)}")
        pre_scan(ev, args)
    else:
        d = args.date or (now - timedelta(days=1)).strftime("%Y-%m-%d")
        today = f"{now:%Y-%m-%d}"
        ev = [e for e in events if (e["date"] == d and e["type"] == "after_close") or (e["date"] == today and e["type"] == "intraday")]
        if not ev:
            L.log(f"{d} 无盘后事件，跳过反应邮件")
            return
        L.log(f"反应邮件：{'、'.join(e['name'] for e in ev)}")
        post_scan(ev, args)


if __name__ == "__main__":
    main()
