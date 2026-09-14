#!/usr/bin/env python3
"""
每日早报（工作日 08:30）：把外围、宏观、事件、昨日 A 股汇总成一句「今日态度」，盘中信号在这个态度下执行。

今日态度（按规则算，不做主观判断）：
    停手   日经或韩国早盘跌 >= 1.5%，或美股隔夜纳指跌 >= 1.5%
    谨慎   日韩或纳指跌 >= 0.8%，或原油单日涨 >= 3%，或美元指数涨 >= 0.5%，或美债 10 年收益率升 >= 8bp，或昨日 A 股 >= 90% 下跌
           谨慎日只做白名单里最强的板块，仓位减半
    常规   以上都没触发
    另标   今天 / 明天的日历事件

数据源：新浪（日韩台港、美股收盘、原油、黄金、离岸人民币）、雅虎（纳指期货、美元指数、美债 10 年，先直连，失败走 .env 的 PROXY）、本地 mins（昨日 A 股）、events.json。

用法：
    python3 scan_brief.py             # 发邮件
    python3 scan_brief.py --no-email  # 只打印
"""
import argparse
import glob
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

WHITELIST = sorted(L.load_whitelist("电子设备,有色金属,农林牧渔"))
STRONG_RET, STRONG_UP = 1.5, 70      # 「走强」：板块等权涨 >= 1.5% 且 >= 70% 上涨
RESON_RET, RESON_UP = 2.0, 80        # 「共振」：>= 2% 且 >= 80%


def sina():
    """返回 {名称: (值, 涨跌%)}"""
    out = {}
    try:
        r = L.http.get("https://hq.sinajs.cn/list=znb_NKY,znb_KOSPI,znb_TWJQ,rt_hkHSI,gb_ixic,gb_dji,gb_$inx,hf_CL,hf_OIL,hf_GC,fx_susdcnh",
                       headers={"User-Agent": L.UA["User-Agent"], "Referer": "https://finance.sina.com.cn/"}, timeout=15)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if '"' not in line:
                continue
            k = line.split("=")[0]
            f = line.split('"')[1].split(",")
            try:
                if "znb_" in k:
                    out[f[0]] = (float(f[1]), float(f[3]))
                elif "hkHSI" in k:
                    out["恒生"] = (float(f[6]), float(f[8]))
                elif "gb_" in k:
                    out[f[0].replace("指数", "")] = (float(f[1]), float(f[2]))
                elif "hf_CL" in k:
                    out["WTI原油"] = (float(f[0]), (float(f[0]) / float(f[7]) - 1) * 100)
                elif "hf_OIL" in k:
                    out["布伦特原油"] = (float(f[0]), (float(f[0]) / float(f[7]) - 1) * 100)
                elif "hf_GC" in k:
                    out["黄金"] = (float(f[0]), (float(f[0]) / float(f[7]) - 1) * 100)
                elif "fx_" in k:
                    out["离岸人民币"] = (float(f[1]), (float(f[1]) / float(f[3]) - 1) * 100 if f[3] else 0.0)
            except (ValueError, IndexError):
                pass
    except Exception as e:
        L.log(f"新浪外围获取失败：{e!r}")
    return out


def yahoo():
    out = {}
    for tk, nm in [("NQ%3DF", "纳指期货"), ("DX-Y.NYB", "美元指数"), ("%5ETNX", "美债10年")]:
        try:
            m = L.yget(f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?range=5d&interval=1d", 20).json()["chart"]["result"][0]["meta"]
            price = m["regularMarketPrice"]
            base = m.get("chartPreviousClose") or m.get("previousClose") or price
            out[nm] = (price, (price / base - 1) * 100, price - base)
        except Exception as e:
            L.log(f"雅虎 {nm} 获取失败：{e!r}")
    return out


def yesterday_ashare():
    days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(L.ROOT, "000001", "*.csv")))[-6:]
    if len(days) < 4:
        return None
    industry = json.load(open(os.path.join(L.META, "industry_em.json")))
    closes = {}
    for p in glob.glob(os.path.join(L.ROOT, "[0-9]*")):
        code = os.path.basename(p)
        if code.startswith("92"):
            continue
        cl = {}
        for dd in days:
            f = os.path.join(p, f"{dd}.csv")
            if os.path.exists(f):
                try:
                    x = pd.read_csv(f, usecols=["close"])
                    if len(x) >= 100:
                        cl[dd] = float(x["close"].iloc[-1])
                except Exception:
                    pass
        if len(cl) >= 3:
            closes[code] = cl
    px = pd.DataFrame(closes).T.reindex(columns=days)
    px = px.loc[:, px.notna().mean() >= 0.8].dropna()   # 剔除不完整的日期
    if px.shape[1] < 4:
        return None
    px = px.iloc[:, -4:]
    ds = list(px.columns)
    d = ds[-1]
    df = pd.DataFrame({"code": px.index, "r1": (px[ds[-1]] / px[ds[-2]] - 1).values * 100, "r3": (px[ds[-1]] / px[ds[0]] - 1).values * 100})
    df["sec"] = df["code"].map(lambda c: industry.get(c, {}).get("em1", "") or "其他")
    cnt = df.groupby("sec")["code"].count()
    df = df[df["sec"].isin(cnt[cnt >= 15].index)]
    g = df.groupby("sec").agg(r1=("r1", "mean"), r3=("r3", "mean"), up=("r1", lambda s: (s > 0).mean() * 100))
    avoid_n = None
    ap = os.path.join(HERE, f"回避名单_{d}.csv")
    if os.path.exists(ap):
        try:
            avoid_n = len(pd.read_csv(ap))
        except Exception:
            pass
    return {"day": d, "mean": df["r1"].mean(), "down_ratio": (df["r1"] < 0).mean() * 100, "n": len(df),
            "top": g.sort_values("r1", ascending=False).head(4), "bottom": g.sort_values("r1").head(4),
            "white": g.loc[[s for s in WHITELIST if s in g.index]], "avoid_n": avoid_n}


def sector_watch(ndays=10):
    """每个板块近 ndays 的等权日涨幅与上涨占比，连续走强/共振天数；给出白名单进出候选"""
    days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(L.ROOT, "000001", "*.csv")))[-(ndays + 1):]
    if len(days) < 4:
        return None
    industry = json.load(open(os.path.join(L.META, "industry_em.json")))
    closes = {}
    for p in glob.glob(os.path.join(L.ROOT, "[0-9]*")):
        code = os.path.basename(p)
        if code.startswith("92"):
            continue
        cl = {}
        for dd in days:
            f = os.path.join(p, f"{dd}.csv")
            if os.path.exists(f):
                try:
                    x = pd.read_csv(f, usecols=["close"])
                    if len(x) >= 100:
                        cl[dd] = float(x["close"].iloc[-1])
                except Exception:
                    pass
        if len(cl) >= len(days) - 2:
            closes[code] = cl
    if not closes:
        return None
    px = pd.DataFrame(closes).T.reindex(columns=days)
    cov = px.notna().mean()
    px = px.loc[:, cov >= 0.8]           # 剔除只有零星股票的日期（盘中或测试写入的当天文件）
    px = px.dropna(thresh=int(px.shape[1] * 0.9))
    days = list(px.columns)
    if len(days) < 4:
        return None
    ret = px.pct_change(axis=1).iloc[:, 1:] * 100
    sec = pd.Series({c: industry.get(c, {}).get("em1", "") or "其他" for c in ret.index})
    cnt = sec.value_counts()
    rows = []
    for s in cnt[cnt >= 15].index:
        if s == "其他":
            continue
        r = ret[sec == s]
        mean = r.mean(); up = (r > 0).mean() * 100
        strong = [(mean.iloc[i] >= STRONG_RET and up.iloc[i] >= STRONG_UP) for i in range(len(mean))]
        reson = [(mean.iloc[i] >= RESON_RET and up.iloc[i] >= RESON_UP) for i in range(len(mean))]
        weak = [(mean.iloc[i] < 0) for i in range(len(mean))]
        def streak(flags):
            n = 0
            for f in reversed(flags):
                if f:
                    n += 1
                else:
                    break
            return n
        rows.append({"sec": s, "n": int(cnt[s]), "r1": mean.iloc[-1], "up1": up.iloc[-1], "r3": r.iloc[:, -3:].sum(axis=1).mean(),
                     "r10": r.sum(axis=1).mean(), "strong_streak": streak(strong), "reson_streak": streak(reson), "weak_streak": streak(weak),
                     "strong_days10": sum(strong), "reson_days10": sum(reson), "in_wl": s in WHITELIST})
    df = pd.DataFrame(rows)
    add = df[(~df["in_wl"]) & (df["strong_streak"] >= 2)].sort_values("strong_streak", ascending=False)
    drop = df[(df["in_wl"]) & (df["weak_streak"] >= 4)]
    return {"df": df, "add": add, "drop": drop, "last_day": days[-1]}


def todays_events(now):
    ev = json.load(open(os.path.join(HERE, "events.json")))["events"]
    today = f"{now:%Y-%m-%d}"
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for e in ev:
        if e["type"] in ("after_close", "macro") and e["date"] == yday:
            out.append(f"昨夜出结果：{e['name']}（07:40 已发反应邮件）")
        elif e["type"] in ("after_close", "macro") and e["date"] == today:
            out.append(f"今晚（北京时间明天凌晨）：{e['name']}，14:00 发埋伏/提醒")
        elif e["type"] == "intraday" and e["date"] == today:
            out.append(f"今天盘中：{e['name']}，留意 {'/'.join(e['chains'])} 板块")
        elif e["type"] in ("after_close", "macro") and e["date"] == tomorrow:
            out.append(f"明晚：{e['name']}")
    return out


def stance(s, y, a):
    reasons_stop, reasons_care = [], []
    nk = s.get("日经225", (0, 0))[1]
    ks = s.get("首尔综合指数", (0, 0))[1]
    nq = s.get("纳斯达克", (0, 0))[1]
    for nm, v in (("日经", nk), ("韩国", ks), ("纳指", nq)):
        if v <= -1.5:
            reasons_stop.append(f"{nm} {v:+.2f}%")
        elif v <= -0.8:
            reasons_care.append(f"{nm} {v:+.2f}%")
    oil = s.get("布伦特原油", s.get("WTI原油", (0, 0)))[1]
    if oil >= 3:
        reasons_care.append(f"原油 {oil:+.1f}%")
    if y.get("美元指数", (0, 0, 0))[1] >= 0.5:
        reasons_care.append(f"美元指数 {y['美元指数'][1]:+.2f}%")
    if y.get("美债10年", (0, 0, 0))[2] >= 0.08:
        reasons_care.append(f"美债10年 +{y['美债10年'][2] * 100:.0f}bp")
    if a and a["down_ratio"] >= 90:
        reasons_care.append(f"昨日 A 股 {a['down_ratio']:.0f}% 下跌")
    if reasons_stop:
        return "停手", "今天不做共振信号。" + "、".join(reasons_stop) + "，外围大跌日共振概率只有 15%。", L.GREEN
    if reasons_care:
        return "谨慎", "只做白名单里最强的板块，仓位减半。触发：" + "、".join(reasons_care) + "。", "#b8742a"
    return "常规", "外围和宏观没有触发条件，按常规扫描执行，白名单：电子设备、有色金属、农林牧渔。", L.RED


def main():
    ap = argparse.ArgumentParser(description="每日早报")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    now = datetime.now()
    s, y, a = sina(), yahoo(), yesterday_ashare()
    w = sector_watch()
    evs = todays_events(now)
    st, why, col = stance(s, y, a)
    if evs:
        why += " 事件：" + "；".join(evs)

    order = ["日经225", "首尔综合指数", "台湾加权指数", "恒生", "纳斯达克", "标普500", "道琼斯", "布伦特原油", "WTI原油", "黄金", "离岸人民币"]
    ov_rows = [(k, s[k][0], s[k][1]) for k in order if k in s] + [(k, v[0], v[1]) for k, v in y.items()]

    lines = [f"{now:%Y-%m-%d %H:%M} 今日态度：{st}", why, "", "外围："]
    lines += [f"  {k:<8} {v:>10.2f}  " + (f"{y['美债10年'][2] * 100:+.0f}bp" if k == "美债10年" else f"{p:+.2f}%") for k, v, p in ov_rows]
    if a:
        lines += ["", f"昨日 A 股（{a['day']}）：均涨 {a['mean']:+.2f}%，{a['down_ratio']:.0f}% 下跌，样本 {a['n']}" + (f"，回避名单 {a['avoid_n']} 只" if a["avoid_n"] is not None else ""),
                  "  最强：" + "  ".join(f"{i} {r.r1:+.2f}%" for i, r in a["top"].iterrows()),
                  "  最弱：" + "  ".join(f"{i} {r.r1:+.2f}%" for i, r in a["bottom"].iterrows()),
                  "  白名单板块近3日：" + "  ".join(f"{i} {r.r3:+.2f}%（昨日 {r.r1:+.2f}%，{r.up:.0f}% 上涨）" for i, r in a["white"].iterrows())]
    if w is not None:
        top = w["df"].sort_values("strong_streak", ascending=False)
        lines += ["", f"板块观察（到 {w['last_day']}）：连续走强天数 = 连续「涨>=1.5% 且 >=70% 上涨」的天数"]
        lines += [f"  {r.sec:<8} 昨日 {r.r1:+.2f}%({r.up1:.0f}%上涨)  近3日 {r.r3:+.2f}%  近10日 {r.r10:+.2f}%  连续走强 {r.strong_streak} 天  10日内走强 {r.strong_days10} 天/共振 {r.reson_days10} 天{'  [白名单]' if r.in_wl else ''}"
                  for r in top.head(8).itertuples()]
        if len(w["add"]):
            lines.append("  ▲ 候选加入白名单（白名单外、连续走强>=2天）：" + "、".join(f"{r.sec}({r.strong_streak}天)" for r in w["add"].itertuples()))
        if len(w["drop"]):
            lines.append("  ▼ 候选移出白名单（连续下跌>=4天）：" + "、".join(f"{r.sec}({r.weak_streak}天)" for r in w["drop"].itertuples()))
    if evs:
        lines += ["", "事件："] + [f"  {e}" for e in evs]
    body = "\n".join(lines)

    parts = [f"<div style='margin-top:12px;padding:12px;border-left:4px solid {col};background:#f7f7f4'><div style='font-size:20px;font-weight:700;color:{col}'>今日态度：{st}</div>"
             f"<div style='margin-top:6px;line-height:1.6'>{why}</div></div>",
             L.h_section("外围", now.strftime("%H:%M"))]
    parts += [f"<div style='padding:7px 0;border-bottom:1px solid {L.LINE};display:flex;justify-content:space-between'><span>{k}</span><span><span style='color:{L.GRAY};margin-right:10px'>{v:,.2f}</span>"
              + (f"<span style='color:{L.RED if y['美债10年'][2] > 0 else L.GREEN};font-weight:600'>{y['美债10年'][2] * 100:+.0f}bp</span>" if k == "美债10年" else L.h_pct(p)) + "</span></div>"
              for k, v, p in ov_rows]
    if a:
        parts.append(L.h_section(f"昨日 A 股 · {a['day'][:4]}-{a['day'][4:6]}-{a['day'][6:]}", f"均涨 {a['mean']:+.2f}% · {a['down_ratio']:.0f}% 下跌" + (f" · 回避名单 {a['avoid_n']} 只" if a["avoid_n"] is not None else "")))
        parts.append("<div style='padding:8px 0;line-height:1.9'>" +
                     "<div><span style='color:%s'>最强</span> %s</div>" % (L.GRAY, "&nbsp;&nbsp;".join(f"{i} {L.h_pct(r.r1)}" for i, r in a["top"].iterrows())) +
                     "<div><span style='color:%s'>最弱</span> %s</div>" % (L.GRAY, "&nbsp;&nbsp;".join(f"{i} {L.h_pct(r.r1)}" for i, r in a["bottom"].iterrows())) + "</div>")
        parts.append(L.h_section("白名单板块", "近 3 日 · 昨日 · 上涨占比"))
        parts += [f"<div style='padding:7px 0;border-bottom:1px solid {L.LINE};display:flex;justify-content:space-between'><span>{i}</span><span>{L.h_pct(r.r3)} <span style='color:{L.GRAY}'>· 昨日</span> {L.h_pct(r.r1)} <span style='color:{L.GRAY}'>· {r.up:.0f}% 上涨</span></span></div>"
                  for i, r in a["white"].iterrows()]
    if w is not None:
        top = w["df"].sort_values(["strong_streak", "r3"], ascending=False).head(8)
        parts.append(L.h_section("板块观察", f"到 {w['last_day'][:4]}-{w['last_day'][4:6]}-{w['last_day'][6:]} · 走强 = 涨≥1.5% 且 ≥70% 上涨"))
        for r in top.itertuples():
            badge = f"<span style='margin-left:6px;padding:1px 6px;border-radius:3px;background:#f0f0ec;color:{L.INK};font-size:11px'>白名单</span>" if r.in_wl else ""
            streak_html = f"<b style='color:{L.RED}'>连续走强 {r.strong_streak} 天</b>" if r.strong_streak >= 2 else f"<span style='color:{L.GRAY}'>连续走强 {r.strong_streak} 天</span>"
            parts.append(f"<div style='padding:8px 0;border-bottom:1px solid {L.LINE}'><div style='display:flex;justify-content:space-between'><span><b>{r.sec}</b>{badge}</span><span>{streak_html}</span></div>"
                         f"<div style='color:{L.GRAY};font-size:12px;margin-top:3px'>昨日 {L.h_pct(r.r1)}（{r.up1:.0f}% 上涨）· 近3日 {L.h_pct(r.r3)} · 近10日 {L.h_pct(r.r10)} · 10日内走强 {r.strong_days10} 天 / 共振 {r.reson_days10} 天</div></div>")
        flags = []
        if len(w["add"]):
            flags.append(f"<div style='margin-top:8px;color:{L.RED}'>▲ 候选加入白名单：" + "、".join(f"{r.sec}（连续走强 {r.strong_streak} 天）" for r in w["add"].itertuples()) + "</div>")
        if len(w["drop"]):
            flags.append(f"<div style='margin-top:4px;color:{L.GREEN}'>▼ 候选移出白名单：" + "、".join(f"{r.sec}（连续下跌 {r.weak_streak} 天）" for r in w["drop"].itertuples()) + "</div>")
        if flags:
            parts.append("".join(flags) + f"<div style='color:{L.GRAY};font-size:12px;margin-top:4px'>改白名单：编辑 sectors.txt，一行一个板块名，扫描器下一轮生效。</div>")
    if evs:
        parts.append(L.h_section("事件", "") + "".join(f"<div style='padding:6px 0'>{e}</div>" for e in evs))
    html = L.h_wrap(f"早报 · {st}", [f"{now:%Y-%m-%d} {now:%H:%M}"], parts,
                    "态度规则：日韩或纳指跌 ≥ 1.5% 停手；日韩纳指跌 ≥ 0.8%、原油涨 ≥ 3%、美元涨 ≥ 0.5%、美债升 ≥ 8bp、昨日 90% 下跌任一触发为谨慎。盘中信号在这个态度下执行。")
    print(body)
    if not args.no_email:
        L.send_mail(f"【早报】今日态度：{st}" + (f"（{'、'.join(e.split('：')[0] for e in evs)}）" if evs else ""), body, html)


if __name__ == "__main__":
    main()
