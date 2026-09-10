#!/usr/bin/env python3
"""
盘中「板块共振 + 大单突破」信号扫描，命中即发邮件。

规则（来自回测，见 README）：
    板块共振  板块（东财一级行业）当日等权涨幅 >= 2% 且 >= 80% 的股票上涨
    个股      在共振板块内，当日涨幅 >= 2%，20 日均成交额 >= 3 亿
    突破      盘中出现单分钟成交量 >= 当日到此刻中位数 8 倍，且该分钟收盘价创当日新高；
              突破发生在最近 --window 分钟内才提示（每只股票每天只提示一次）
    前期龙头  现价距 60 日最高价回撤 >= 25%（标记，不作为过滤）
    外围过滤  日经 225 或韩国综合当天跌幅 >= 1.5% 的日子不出信号（回测：这类日子极少共振，勉强做为负收益）
    卖出      次日 10:00 前，早盘冲高即走；脚本会在次日 09:31 左右发一封卖出提醒

数据源：腾讯批量行情（全市场涨幅，4 秒）+ 腾讯 1 分钟 K 线（候选股，逐只）；
        20 日均额 / 60 日高点来自本地 mins/ 分钟库，缓存在 mins/_meta/daily_cache.json。
邮件：  读取同目录 .env 里的 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM/ALERT_TO（多个收件人用逗号隔开）。

用法：
    python3 scan_live.py                 # 盘中由 launchd 每 1 分钟调用；非交易时段直接退出；文件锁防止重叠
    python3 scan_live.py --now           # 忽略时段限制，用最新数据立刻扫一遍（收盘后复盘用）
    python3 scan_live.py --now --no-email
    python3 scan_live.py --build-cache   # 重建 20 日均额 / 60 日高点缓存（每天 mins_sync 之后跑）
    python3 scan_live.py --test-email
可调参数：--min-sector 2 --min-up 80 --min-gain 2 --min-amt 3 --spike 8 --window 15
"""
import argparse
import csv
import glob
import json
import os
import smtplib
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, date, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "mins")
META = os.path.join(ROOT, "_meta")
LOGS = os.path.join(ROOT, "_logs")
CACHE = os.path.join(META, "daily_cache.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
http = requests.Session()
http.trust_env = False


def log(msg):
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, f"live_{date.today():%Y%m%d}.log"), "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- 邮件
def load_env():
    p = os.path.join(HERE, ".env")
    if not os.path.exists(p):
        return {}
    return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in open(p) if "=" in l and not l.startswith("#"))}


def send_mail(subject, body, html=None):
    env = load_env()
    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_TO"]
    if any(k not in env for k in need):
        log("未配置 .env 邮件参数，跳过发信")
        return False
    sender = env.get("SMTP_FROM") or env["SMTP_USER"]
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("板块信号", "utf-8")), sender))
    recipients = [x.strip() for x in env["ALERT_TO"].replace("；", ",").replace(";", ",").split(",") if x.strip()]
    msg["To"] = ", ".join(recipients)
    port = int(env["SMTP_PORT"])
    for attempt in range(3):
        try:
            cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
            with cls(env["SMTP_HOST"], port, timeout=30) as s:
                if port != 465:
                    s.ehlo()
                    s.starttls()
                s.login(env["SMTP_USER"], env["SMTP_PASS"])
                s.sendmail(sender, recipients, msg.as_string())
            log(f"邮件已发送：{subject}")
            return True
        except Exception as e:
            log(f"发信失败({attempt + 1}/3)：{e!r}")
            time.sleep(3)
    return False


# ---------------------------------------------------------------- HTML 邮件排版（手机优先，内联样式）
RED, GREEN, GRAY, INK, LINE = "#d23f31", "#1f9d55", "#7a7f85", "#1f2429", "#e6e6e3"


def h_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def h_pct(v, digits=2):
    """涨跌幅：红涨绿跌"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return f'<span style="color:{GRAY}">—</span>'
    c = RED if v > 0 else GREEN if v < 0 else GRAY
    return f'<span style="color:{c};font-weight:600">{v:+.{digits}f}%</span>'


def h_wrap(title, meta_lines, parts, note=None):
    meta = "".join(f'<div style="color:{GRAY};font-size:13px;line-height:1.6">{m}</div>' for m in meta_lines)
    note_html = (f'<div style="margin-top:18px;padding:10px 12px;background:#f5f5f2;border-radius:6px;color:{GRAY};font-size:12px;line-height:1.6">{note}</div>'
                 if note else "")
    return f'''<div style="max-width:640px;margin:0 auto;padding:14px 12px;font-family:-apple-system,'PingFang SC','Helvetica Neue',Arial,sans-serif;color:{INK};font-size:14px">
<div style="font-size:18px;font-weight:700;line-height:1.3;margin-bottom:6px">{title}</div>
{meta}
{"".join(parts)}
{note_html}
</div>'''


def h_section(title, right=""):
    return (f'<div style="margin-top:18px;padding-bottom:6px;border-bottom:2px solid {INK};display:flex;justify-content:space-between;align-items:baseline">'
            f'<span style="font-size:16px;font-weight:700">{title}</span><span style="color:{GRAY};font-size:12px">{right}</span></div>')


def h_card(line1_left, line1_right, line2, line3="", badge=""):
    """一只股票一张卡：第一行 代码名称 | 涨幅；第二行 关键数字；第三行 次要信息"""
    b = f'<span style="margin-left:6px;padding:1px 6px;border-radius:3px;background:#fff1ef;color:{RED};font-size:11px">{badge}</span>' if badge else ""
    l3 = f'<div style="color:{GRAY};font-size:12px;margin-top:3px">{line3}</div>' if line3 else ""
    return (f'<div style="padding:10px 0;border-bottom:1px solid {LINE}">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="font-weight:700;font-size:15px">{line1_left}{b}</span><span style="font-size:15px">{line1_right}</span></div>'
            f'<div style="margin-top:4px;font-size:13px;line-height:1.5">{line2}</div>{l3}</div>')


def h_kv(*pairs):
    """关键数字：标签灰、数值黑，用两个空格隔开"""
    return "&nbsp;&nbsp;".join(f'<span style="color:{GRAY}">{k}</span> <b>{v}</b>' for k, v in pairs)


# ---------------------------------------------------------------- 本地缓存：20 日均额 / 60 日高点
def _stock_stats(code):
    files = sorted(glob.glob(os.path.join(ROOT, code, "*.csv")))[-60:]
    if len(files) < 20:
        return code, None
    amts, highs = [], []
    for f in files:
        try:
            x = pd.read_csv(f, usecols=["high", "amount"])
        except Exception:
            continue
        if len(x) < 100:
            continue
        amts.append(float(x["amount"].sum()))
        highs.append(float(x["high"].max()))
    if len(amts) < 20:
        return code, None
    return code, {"avg20": float(np.mean(amts[-20:])), "high60": float(max(highs)), "date": os.path.basename(files[-1])[:8]}


def build_cache():
    codes = sorted(os.path.basename(d) for d in glob.glob(os.path.join(ROOT, "[0-9]*")) if not os.path.basename(d).startswith("92"))
    log(f"重建缓存：{len(codes)} 只")
    out = {}
    with ProcessPoolExecutor() as ex:
        for code, st in ex.map(_stock_stats, codes, chunksize=50):
            if st:
                out[code] = st
    os.makedirs(META, exist_ok=True)
    json.dump({"built": f"{date.today():%Y%m%d}", "stocks": out}, open(CACHE, "w"))
    log(f"缓存完成：{len(out)} 只")
    return out


def load_cache():
    if os.path.exists(CACHE):
        c = json.load(open(CACHE))
        return c["stocks"]
    return build_cache()


# ---------------------------------------------------------------- 行情
def tencent_symbol(code):
    return ("sh" if code.startswith(("6", "9")) else "bj" if code.startswith(("4", "8")) else "sz") + code


def batch_quotes(codes):
    """返回 {code: {name, price, pct, amount(元), time}}"""
    out = {}
    syms = [tencent_symbol(c) for c in codes]
    for i in range(0, len(syms), 80):
        for attempt in range(3):
            try:
                r = http.get("https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 80]), headers=UA, timeout=20)
                r.encoding = "gbk"
                for line in r.text.split(";"):
                    if "=" not in line:
                        continue
                    f = line.split("=")[1].strip('"\n ').split("~")
                    if len(f) < 50:
                        continue
                    try:
                        out[f[2]] = {"name": f[1], "price": float(f[3]), "pct": float(f[32]), "amount": float(f[37]) * 1e4, "time": f[30],
                                     "prev_close": float(f[4]) if f[4] else 0.0, "low": float(f[34]) if f[34] else 0.0, "high": float(f[33]) if f[33] else 0.0}
                    except ValueError:
                        pass
                break
            except Exception:
                time.sleep(1 + attempt)
    return out


def fetch_m1_today(code, day):
    sym = tencent_symbol(code)
    for attempt in range(3):
        try:
            bars = http.get(f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m1,,320", headers=UA, timeout=15).json()["data"][sym]["m1"]
            return [(b[0][8:10] + ":" + b[0][10:12], float(b[2]), float(b[3]), float(b[5])) for b in bars if b[0].startswith(day)]  # time, close, high, vol(手)
        except Exception:
            time.sleep(1 + attempt)
    return None


def overseas():
    """新浪实时：日经225 / 韩国综合 当天涨跌幅，纳指最近一个交易日涨跌幅。失败返回 {}"""
    try:
        r = http.get("https://hq.sinajs.cn/list=znb_NKY,znb_KOSPI,gb_ixic",
                     headers={"User-Agent": UA["User-Agent"], "Referer": "https://finance.sina.com.cn/"}, timeout=15)
        r.encoding = "gbk"
        out = {}
        for line in r.text.strip().split("\n"):
            f = line.split('"')[1].split(",")
            if "NKY" in line:
                out["日经"] = float(f[3])
            elif "KOSPI" in line:
                out["韩国"] = float(f[3])
            elif "ixic" in line:
                out["纳指"] = float(f[2])
        return out
    except Exception as e:
        log(f"外围指数获取失败：{e!r}")
        return {}


def find_breakout(bars, spike):
    """返回 [(time, close, vol, multiple)]：分钟量 >= spike 倍当日到此刻中位数 且 收盘 >= 此前当日最高"""
    res = []
    v = np.array([b[3] for b in bars])
    c = np.array([b[1] for b in bars])
    h = np.array([b[2] for b in bars])
    run_high = np.maximum.accumulate(h)
    for i in range(5, len(bars)):
        med = max(np.median(v[:i]), 1)
        if v[i] >= spike * med and c[i] >= run_high[i - 1]:
            res.append((bars[i][0], c[i], v[i], v[i] / med))
    return res


# ---------------------------------------------------------------- 主逻辑
def in_trading_window(now):
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "09:35" <= t <= "11:30" or "13:00" <= t <= "14:30"


def minutes_between(t1, t2):
    h1, m1 = map(int, t1.split(":"))
    h2, m2 = map(int, t2.split(":"))
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def sell_reminder(now, args, ov=None, force=False):
    """次日早盘卖出提醒：09:35 第一封、09:55 最后提醒。逐只拉实时价，给出明确处理结论。"""
    ov = ov or {}
    t = now.strftime("%H:%M")
    which = 1 if "09:30" <= t <= "09:45" else 2 if "09:52" <= t <= "09:59" else None
    if force:
        which = which or 1
    if which is None:
        return
    files = sorted(glob.glob(os.path.join(META, "live_signals_*.json")))
    files = [f for f in files if not f.endswith(f"{now:%Y%m%d}.json")]
    if not files:
        return
    last = files[-1]
    data = json.load(open(last))
    key = "sell_reminded" if which == 1 else "sell_reminded2"
    if not data.get("signals") or (data.get(key) and not force):
        return
    sigs = data["signals"]
    q = batch_quotes([x["code"] for x in sigs])

    worst = min([v for k, v in ov.items() if k in ("日经", "韩国")] + [0])
    nq = ov.get("纳指", 0)
    ov_bad = worst <= -1.5 or nq <= -1.5
    ov_line = "  ".join(f"{k} {v:+.2f}%" for k, v in ov.items()) or "外围数据缺失"

    rows = []
    for x in sigs:
        s_ = q.get(x["code"], {})
        price, high, open_ = s_.get("price", 0), s_.get("high", 0), s_.get("prev_close", 0)
        prem = (price / x["price"] - 1) * 100 if price else None
        prem_high = (high / x["price"] - 1) * 100 if high else None
        if ov_bad:
            act, col = "开盘直接卖", GREEN
        elif prem is None:
            act, col = "无行情，按 10:00 前离场执行", GRAY
        elif prem_high is not None and prem_high <= 0:
            act, col = "无溢价，直接卖", GREEN
        elif prem > 0:
            act, col = f"有溢价 {prem:+.1f}%，现在卖", RED
        else:
            act, col = f"早盘曾冲高 {prem_high:+.1f}% 现已回落，直接卖", "#b8742a"
        rows.append(dict(x, now=price, prem=prem, prem_high=prem_high, act=act, col=col, pct=s_.get("pct")))

    n_prem = sum(1 for r in rows if r["prem"] is not None and r["prem"] > 0)
    if ov_bad:
        headline, hcol = "外围大跌，全部开盘直接卖，不等冲高", GREEN
    elif n_prem == 0:
        headline, hcol = "没有一只有溢价，全部直接卖", GREEN
    elif n_prem == len(rows):
        headline, hcol = "都有溢价，现在就卖，别拿过 10:00", RED
    else:
        headline, hcol = f"{n_prem} 只有溢价现在卖，其余直接卖", "#b8742a"
    if which == 2:
        headline = "最后提醒 · " + headline

    lines = [f"{'最后提醒 ' if which == 2 else ''}昨日({data['date']})信号，{now:%H:%M} 实时：", f"处理：{headline}", f"外围：{ov_line}", ""]
    for r in rows:
        lines.append(f"  {r['code']} {r['name']}  信号价 {r['price']}  现价 {r['now'] or '-'}  相对信号价 {'-' if r['prem'] is None else f'{r['prem']:+.2f}%'}  "
                     f"早盘最高 {'-' if r['prem_high'] is None else f'{r['prem_high']:+.2f}%'}  → {r['act']}")
    parts = [f"<div style='margin-top:12px;padding:12px;border-left:4px solid {hcol};background:#f7f7f4'><div style='font-size:17px;font-weight:700;color:{hcol}'>{headline}</div>"
             f"<div style='color:{GRAY};font-size:12px;margin-top:4px'>外围 " + (" · ".join(f"{k} {h_pct(v)}" for k, v in ov.items()) or "数据缺失") + f" · {now:%H:%M} 实时价</div></div>",
             h_section("逐只处理", f"{len(rows)} 只")]
    for r in rows:
        parts.append(h_card(f"{r['name']} <span style='color:{GRAY};font-weight:400;font-size:12px'>{r['code']}</span>",
                            f"<span style='color:{r['col']};font-weight:700'>{r['act']}</span>",
                            h_kv(("信号价", f"{r['price']:g}"), ("现价", f"{r['now']:g}" if r['now'] else "-"), ("相对信号价", h_pct(r['prem']) if r['prem'] is not None else "-")),
                            h_kv(("早盘最高", h_pct(r['prem_high']) if r['prem_high'] is not None else "-"), ("今日", h_pct(r['pct']) if r['pct'] is not None else "-"), ("板块", r["sector"]))))
    html = h_wrap(("最后提醒 · " if which == 2 else "") + "卖出提醒 · 10:00 前离场",
                  [f"昨日（{data['date'][:4]}-{data['date'][4:6]}-{data['date'][6:]}）信号"], parts,
                  "溢价集中在开盘后 30 分钟，10:00 之后只会更差；亏损单一样走，不等回本。")
    subj = f"【{'最后提醒' if which == 2 else '卖出提醒'}】{headline}（昨日 {len(rows)} 只）"
    print("\n".join(lines))
    if not args.no_email:
        send_mail(subj, "\n".join(lines), html)
    if not force:
        data[key] = True
        json.dump(data, open(last, "w"), ensure_ascii=False)


def scan(args):
    now = datetime.now()
    today = f"{now:%Y%m%d}"
    if not args.now and not in_trading_window(now):
        return
    ov = overseas()
    ov_line = "  ".join(f"{k} {v:+.2f}%" for k, v in ov.items()) or "外围数据缺失"
    sell_reminder(now, args, ov)
    if not args.ignore_overseas:
        bad = [k for k in ("日经", "韩国") if ov.get(k, 0) <= -args.max_overseas_drop]
        if bad:
            log(f"外围大跌（{ov_line}），{'/'.join(bad)} 跌幅超过 {args.max_overseas_drop}%，按规则今天停手")
            return

    industry = json.load(open(os.path.join(META, "industry_em.json")))
    stocklist = json.load(open(os.path.join(META, "stocklist.json")))["stocks"]
    codes = [s["code"] for s in stocklist if s["ex"] != "bj"]
    cache = load_cache()

    q = batch_quotes(codes)
    if not q:
        log("行情获取失败")
        return
    # 交易日判断：以 000001 的行情时间为准
    qday = q.get("000001", {}).get("time", "")[:8]
    if not args.now and qday != today:
        log(f"行情日期 {qday} 不是今天，非交易日，退出")
        return
    day = qday if args.now else today

    df = pd.DataFrame.from_dict(q, orient="index")
    df.index.name = "code"
    df = df.reset_index()
    df["sec"] = df["code"].map(lambda c: industry.get(c, {}).get("em1", "") or "其他")
    cnt = df.groupby("sec")["code"].count()
    df.loc[df["sec"].isin(cnt[cnt < 15].index), "sec"] = "其他"
    df = df[df["pct"].abs() < 30]  # 剔除异常
    secstat = df.groupby("sec").agg(mean=("pct", "mean"), up=("pct", lambda s: (s > 0).mean() * 100), n=("pct", "count"))
    resonant = secstat[(secstat["mean"] >= args.min_sector) & (secstat["up"] >= args.min_up) & (secstat.index != "其他")]
    mkt = df["pct"].mean()
    log(f"{day} {now:%H:%M} 外围：{ov_line}")
    log(f"{day} {now:%H:%M} 全市场均涨 {mkt:+.2f}%  共振板块 {len(resonant)} 个：" +
        "  ".join(f"{s}({r['mean']:+.1f}%,{r['up']:.0f}%上涨)" for s, r in resonant.iterrows()))
    if resonant.empty:
        return
    if not args.all_sectors:
        allow = {x.strip() for x in args.sectors.split(",") if x.strip()}
        skipped = [x for x in resonant.index if x not in allow]
        resonant = resonant[resonant.index.isin(allow)]
        if skipped:
            log(f"不在白名单的共振板块跳过：{'、'.join(skipped)}")
        if resonant.empty:
            return

    state_path = os.path.join(META, f"live_signals_{day}.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"date": day, "signals": [], "checked": {}}
    state.setdefault("follow", [])
    done = {s["code"] for s in state["signals"]}
    follow_done = {f["code"] for f in state["follow"]}

    cand = df[df["sec"].isin(resonant.index) & (df["pct"] >= args.min_gain)].copy()
    cand["avg20"] = cand["code"].map(lambda c: cache.get(c, {}).get("avg20", 0))
    cand["high60"] = cand["code"].map(lambda c: cache.get(c, {}).get("high60", np.nan))
    cand = cand[(cand["avg20"] >= args.min_amt * 1e8) & ~cand["code"].isin(done)]
    log(f"候选 {len(cand)} 只（共振板块内 涨幅>={args.min_gain}% 20日均额>={args.min_amt}亿），拉分钟线检查突破...")
    if cand.empty:
        return

    with ThreadPoolExecutor(8) as ex:
        bars_list = list(ex.map(lambda c: fetch_m1_today(c, day), cand["code"]))

    now_t = now.strftime("%H:%M")
    new = []
    for (_, s), bars in zip(cand.iterrows(), bars_list):
        if not bars or len(bars) < 6:
            continue
        br = find_breakout(bars, args.spike)
        if not br:
            continue
        last_t = bars[-1][0]
        ref_t = last_t if args.now else now_t
        recent = [b for b in br if minutes_between(b[0], ref_t) <= args.window]
        if not recent and not args.now:
            continue
        b = recent[-1] if recent else br[-1]
        dd60 = (s["price"] / s["high60"] - 1) * 100 if s["high60"] and s["high60"] > 0 else np.nan
        new.append({
            "code": s["code"], "name": s["name"], "sector": s["sec"], "price": s["price"], "pct": round(s["pct"], 2),
            "breakout_time": b[0], "breakout_price": b[1], "minute_vol": int(b[2]), "spike_x": round(b[3], 1),
            "first_breakout": br[0][0], "n_breakouts": len(br),
            "avg20_yi": round(s["avg20"] / 1e8, 1), "dd60": round(dd60, 1) if not np.isnan(dd60) else None,
            "leader": bool(dd60 <= -25) if not np.isnan(dd60) else False,
            "sector_pct": round(float(resonant.loc[s["sec"], "mean"]), 2), "sector_up": round(float(resonant.loc[s["sec"], "up"]), 0),
            "signal_time": now_t if not args.now else last_t,
        })

    if args.leaders_only:
        new = [x for x in new if x["leader"]]

    # 3. 联动候选：板块内今日已有 >=2 只突破后，其余处于共振、涨>=2%、距日内高点<=1% 的活跃股
    all_sig = state["signals"] + new
    sec_count = pd.Series([x["sector"] for x in all_sig]).value_counts() if all_sig else pd.Series(dtype=int)
    follow = []
    sig_codes = {x["code"] for x in all_sig}
    for sec in sec_count[sec_count >= 2].index:
        pool = df[(df["sec"] == sec) & (df["pct"] >= args.min_gain) & (df["high"] > 0)]
        pool = pool[(pool["price"] / pool["high"] - 1) * 100 >= -1]
        for _, s in pool.iterrows():
            if s["code"] in sig_codes or s["code"] in follow_done:
                continue
            a20 = cache.get(s["code"], {}).get("avg20", 0)
            if a20 < args.min_amt * 1e8:
                continue
            h60 = cache[s["code"]].get("high60") or 0
            dd60 = (s["price"] / h60 - 1) * 100 if h60 > 0 else np.nan
            follow.append({"code": s["code"], "name": s["name"], "sector": sec, "price": s["price"], "pct": round(s["pct"], 2),
                           "dist_high": round((s["price"] / s["high"] - 1) * 100, 2), "avg20_yi": round(a20 / 1e8, 1),
                           "dd60": round(dd60, 1) if not np.isnan(dd60) else None, "leader": bool(dd60 <= -25) if not np.isnan(dd60) else False,
                           "signal_time": now_t if not args.now else "复盘", "n_breakouts": int(sec_count[sec])})
    follow.sort(key=lambda r: (r["sector"], not r["leader"], -r["pct"]))

    if not new and not follow:
        log("本轮无新突破")
        return
    new.sort(key=lambda r: (r["sector"], not r["leader"], -r["spike_x"]))
    state["signals"] += new
    state["follow"] += follow
    json.dump(state, open(state_path, "w"), ensure_ascii=False)
    if new:
        csv_path = os.path.join(HERE, f"信号_{day}.csv")
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(new[0].keys()))
            if write_header:
                w.writeheader()
            w.writerows(new)
    if follow:
        fcsv = os.path.join(HERE, f"联动候选_{day}.csv")
        write_header = not os.path.exists(fcsv)
        with open(fcsv, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(follow[0].keys()))
            if write_header:
                w.writeheader()
            w.writerows(follow)

    RULE = ("规则：次日 10:00 前卖出，早盘冲高即走。严格回测（买入时点口径）：电子设备 +0.86%/57%，有色 +1.24%/58%，农林牧渔 +1.85%/64%；"
            "前期龙头 +0.54%/54%，非前期龙头 -0.77%/42%；14:30 后入场为负。联动候选（板块第 2 只突破后买高点附近的其余股）+0.82%/57%。未扣费，样本 31 个交易日。这是信号，不是买入建议。")
    lines = [f"扫描时间 {day} {now:%H:%M}，全市场均涨 {mkt:+.2f}%", f"外围：{ov_line}" + ("  ⚠ 隔夜纳指跌超1.5%，注意早盘溢价可能偏弱" if ov.get("纳指", 0) <= -1.5 else ""), ""]
    for sec, grp in pd.DataFrame(new).groupby("sector") if new else []:
        r = resonant.loc[sec]
        lines.append(f"■ {sec}  板块 {r['mean']:+.2f}%  {r['up']:.0f}% 上涨  ({int(r['n'])} 只)")
        for x in grp.itertuples():
            tag = "  [前期龙头]" if x.leader else "  [非前期龙头，历史偏弱]"
            lines.append(f"  {x.code} {x.name}  现价 {x.price}  涨 {x.pct:+.2f}%  突破 {x.breakout_time} @ {x.breakout_price}  "
                         f"分钟量 {x.minute_vol} 手 = {x.spike_x} 倍  首次突破 {x.first_breakout}  20日均额 {x.avg20_yi} 亿  距60日高 {x.dd60}%{tag}")
        lines.append("")
    for sec, grp in pd.DataFrame(follow).groupby("sector") if follow else []:
        lines.append(f"▲ 联动候选 {sec}（板块今日已 {grp.iloc[0]['n_breakouts']} 只突破，以下在日内高点 1% 以内、尚未突破）")
        for x in grp.itertuples():
            lines.append(f"  {x.code} {x.name}  现价 {x.price}  涨 {x.pct:+.2f}%  距日内高 {x.dist_high}%  20日均额 {x.avg20_yi} 亿  距60日高 {x.dd60}%" + ("  [前期龙头]" if x.leader else ""))
        lines.append("")
    lines.append(RULE)
    body = "\n".join(lines)
    parts = []
    for sec, grp in pd.DataFrame(new).groupby("sector") if new else []:
        r = resonant.loc[sec]
        parts.append(h_section(f"{sec} {h_pct(r['mean'])}", f"{r['up']:.0f}% 上涨 · {int(r['n'])} 只"))
        for x in grp.itertuples():
            parts.append(h_card(
                f"{x.name} <span style='color:{GRAY};font-weight:400;font-size:12px'>{x.code}</span>", h_pct(x.pct),
                h_kv(("突破", f"{x.breakout_time} @ {x.breakout_price:g}"), ("分钟量", f"{x.spike_x} 倍"), ("现价", f"{x.price:g}")),
                h_kv(("首次突破", x.first_breakout), ("20日均额", f"{x.avg20_yi} 亿"), ("距60日高", f"{x.dd60}%")) +
                ("" if x.leader else f" <span style='color:{GRAY}'>非前期龙头，历史偏弱</span>"),
                "前期龙头" if x.leader else ""))
    for sec, grp in pd.DataFrame(follow).groupby("sector") if follow else []:
        parts.append(h_section(f"联动候选 · {sec}", f"板块今日已 {grp.iloc[0]['n_breakouts']} 只突破 · 高点 1% 以内、尚未突破"))
        for x in grp.itertuples():
            parts.append(h_card(f"{x.name} <span style='color:{GRAY};font-weight:400;font-size:12px'>{x.code}</span>", h_pct(x.pct),
                                h_kv(("现价", f"{x.price:g}"), ("距日内高", f"{x.dist_high}%"), ("20日均额", f"{x.avg20_yi} 亿")),
                                h_kv(("距60日高", f"{x.dd60}%")), "前期龙头" if x.leader else ""))
    ov_warn = " · <span style='color:#b8742a'>隔夜纳指跌超 1.5%，早盘溢价可能偏弱</span>" if ov.get("纳指", 0) <= -1.5 else ""
    html = h_wrap(f"板块共振信号 · {now:%H:%M}",
                  [f"{day[:4]}-{day[4:6]}-{day[6:]} · 全市场均涨 {h_pct(mkt)} · 共振板块 {len(resonant)} 个", f"外围 {ov_line}{ov_warn}"],
                  parts, RULE)
    log("新信号 %d 只：%s | 联动候选 %d 只" % (len(new), " ".join(f"{x['code']}{x['name']}" for x in new), len(follow)))
    print(body)
    if not args.no_email:
        names_ = [x["name"] for x in new[:6]] or [x["name"] for x in follow[:6]]
        send_mail(f"【板块共振信号】{now:%H:%M} 突破 {len(new)} 只 联动 {len(follow)} 只：" + "、".join(names_) + ("…" if len(new) > 6 else ""), body, html)


def acquire_lock():
    """同一时刻只允许一个扫描进程（launchd 每分钟触发，防止上一轮没跑完又起一轮）"""
    import fcntl
    os.makedirs(META, exist_ok=True)
    fh = open(os.path.join(META, "live.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def main():
    ap = argparse.ArgumentParser(description="盘中板块共振+大单突破扫描")
    ap.add_argument("--now", action="store_true", help="忽略交易时段，用最新数据立即扫描")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--build-cache", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    ap.add_argument("--test-sell-reminder", action="store_true", help="用最近一天的信号立刻发一封卖出提醒（不改状态）")
    ap.add_argument("--min-sector", type=float, default=2.0, help="板块等权涨幅下限 %%")
    ap.add_argument("--min-up", type=float, default=80.0, help="板块上涨家数占比下限 %%")
    ap.add_argument("--min-gain", type=float, default=2.0, help="个股当日涨幅下限 %%")
    ap.add_argument("--min-amt", type=float, default=3.0, help="20 日均成交额下限（亿）")
    ap.add_argument("--spike", type=float, default=8.0, help="分钟量相对当日中位数的倍数")
    ap.add_argument("--window", type=int, default=15, help="突破发生在最近 N 分钟内才提示")
    ap.add_argument("--max-overseas-drop", type=float, default=1.5, help="日经或韩国当天跌幅达到此值(%%)则停手，默认 1.5")
    ap.add_argument("--sectors", default="电子设备,有色金属,农林牧渔", help="只对这些板块发信号（严格回测为正的板块），逗号分隔")
    ap.add_argument("--all-sectors", action="store_true", help="不限板块（国防/电气/机械/交运/信息技术历史为负）")
    ap.add_argument("--leaders-only", action="store_true", help="只发前期龙头（距60日高<=-25%%）的信号")
    ap.add_argument("--ignore-overseas", action="store_true", help="不做外围大跌过滤")
    args = ap.parse_args()
    if args.build_cache:
        build_cache()
        return
    if args.test_email:
        html = h_wrap("测试 · 板块共振信号", ["邮件通道正常，这是 HTML 排版样例"],
                      [h_section(f"电子设备 {h_pct(3.65)}", "91% 上涨 · 561 只"),
                       h_card(f"德明利 <span style='color:{GRAY};font-weight:400;font-size:12px'>001309</span>", h_pct(7.19),
                              h_kv(("突破", "10:46 @ 434.6"), ("分钟量", "11.2 倍"), ("现价", "435.4")),
                              h_kv(("首次突破", "10:44"), ("20日均额", "99.0 亿"), ("距60日高", "-56%")), "前期龙头")],
                      "卖出规则：次日 10:00 前离场，早盘冲高即走。")
        ok = send_mail("【测试】板块共振信号", "邮件通道正常。", html)
        sys.exit(0 if ok else 1)
    if args.test_sell_reminder:
        sell_reminder(datetime.now(), args, overseas(), force=True)
        return
    lock = acquire_lock()
    if lock is None:
        return  # 上一轮还在跑
    t0 = time.time()
    scan(args)
    if in_trading_window(datetime.now()) or args.now:
        log(f"本轮耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
