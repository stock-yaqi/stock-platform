#!/usr/bin/env python3
"""
极小的持仓回执服务：邮件里点「我已买入」就打开这个页面，把票记进 positions.json，
次日由 scan_hold.py 做 1 分钟冲高监控。由 scheduler.py 常驻拉起，监听 0.0.0.0:8085
（路由器 NAT 8085 → 192.168.3.21:8085，手机在外网也能点）。

路由（都要带 ?k=<WEB_TOKEN>，.env 里配；没配就不校验但只允许内网访问）：
    /            持仓页：现价、盈亏、卖出按钮
    /buy?c=代码   记一笔买入（买入价取点击瞬间的实时价，页面上可改）
    /sell?c=代码  标记已卖出，停止监控
    /price?c=&v=  改买入价
    /del?c=代码   删除这笔（点错了）
    /health      存活检查，不需要 token

用法：
    python3 webhook.py                  # 前台跑，Ctrl-C 停
    python3 webhook.py --port 8085
"""
import argparse
import json
import os
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_live as L  # noqa: E402
import positions as P  # noqa: E402

LOCK = threading.Lock()
RED, GREEN, GRAY, INK, LINE = L.RED, L.GREEN, L.GRAY, L.INK, L.LINE


def log(msg):
    os.makedirs(L.LOGS, exist_ok=True)
    line = f"{datetime.now():%m-%d %H:%M:%S} [webhook] {msg}"
    print(line, flush=True)
    with open(os.path.join(L.LOGS, "webhook.log"), "a") as f:
        f.write(line + "\n")


def page(title, body, token=""):
    k = f"?k={token}" if token else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;background:#f5f5f2;font-family:-apple-system,'PingFang SC','Helvetica Neue',Arial,sans-serif;color:{INK}">
<div style="max-width:560px;margin:0 auto;padding:16px 14px">
<div style="font-size:19px;font-weight:700;margin-bottom:12px">{title}</div>
<div style="background:#fff;border-radius:10px;padding:14px">{body}</div>
<div style="margin-top:14px;text-align:center"><a href="/{k}" style="color:{GRAY};font-size:13px">查看全部持仓</a></div>
</div></body></html>"""


def btn(href, text, bg=INK, fg="#fff"):
    return (f'<a href="{href}" style="display:inline-block;padding:8px 16px;border-radius:6px;background:{bg};'
            f'color:{fg};text-decoration:none;font-size:14px;margin:4px 6px 4px 0">{text}</a>')


def pct_html(v, digits=2):
    if v is None:
        return f'<span style="color:{GRAY}">—</span>'
    c = RED if v > 0 else GREEN if v < 0 else GRAY
    return f'<span style="color:{c};font-weight:700">{v:+.{digits}f}%</span>'


def position_rows(token):
    rows = P.holding()
    if not rows:
        return f'<div style="color:{GRAY}">当前没有持仓。共振邮件里点「我已买入」就会记到这里。</div>'
    q = L.batch_quotes([p["code"] for p in rows])
    today = f"{datetime.now():%Y%m%d}"
    out = []
    for p in rows:
        sell_u = f"/sell?c={p['code']}&k={token}"
        del_u = f"/del?c={p['code']}&k={token}"
        s = q.get(p["code"], {})
        now_p, bp = s.get("price"), p.get("buy_price")
        pnl = (now_p / bp - 1) * 100 if now_p and bp else None
        watch = "次日起 1 分钟冲高监控中" if p["buy_date"] < today else "明天 09:30 起 1 分钟冲高监控"
        out.append(
            f'<div style="padding:12px 0;border-bottom:1px solid {LINE}">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<span style="font-weight:700;font-size:16px"><a href="{L.stock_url(p["code"])}" style="color:{INK};text-decoration:none">{p["name"]}</a> '
            f'<span style="color:{GRAY};font-weight:400;font-size:12px">{p["code"]}</span></span>'
            f'<span style="font-size:16px">{pct_html(pnl)}</span></div>'
            f'<div style="margin-top:4px;font-size:13px;color:{GRAY}">买入 {p["buy_date"][4:6]}-{p["buy_date"][6:]} {p["buy_time"]} @ '
            f'<b style="color:{INK}">{bp}</b>&nbsp;&nbsp;现价 <b style="color:{INK}">{now_p or "-"}</b>&nbsp;&nbsp;{p.get("sector", "")}</div>'
            f'<div style="margin-top:3px;font-size:12px;color:{GRAY}">{watch}</div>'
            f'<div style="margin-top:6px">{btn(sell_u, "我已卖出", GREEN)}'
            f'{btn(del_u, "点错了，删除", "#fff", GRAY)}'
            f'<form action="/price" method="get" style="display:inline-block;margin-left:6px">'
            f'<input type="hidden" name="c" value="{p["code"]}"><input type="hidden" name="k" value="{token}">'
            f'<input name="v" type="number" step="0.01" placeholder="改买入价" style="width:96px;padding:7px;border:1px solid {LINE};border-radius:6px;font-size:14px">'
            f'<button style="padding:7px 12px;margin-left:4px;border:1px solid {LINE};background:#fff;border-radius:6px;font-size:14px">改价</button></form>'
            f'</div></div>')
    return "".join(out)


class H(BaseHTTPRequestHandler):
    server_version = "stock-a"

    def _send(self, html, code=200):
        b = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        if path == "/health":
            self._send("ok")
            return
        if path == "/favicon.ico":
            self._send("", 404)
            return
        token = self.server.token
        given = (qs.get("k") or [""])[0]
        client = self.client_address[0]
        if token and given != token:
            log(f"拒绝 {client} {self.path[:80]}")
            self._send(page("无权访问", "链接不对或已失效。请从邮件里的按钮打开。"), 403)
            return
        code = (qs.get("c") or [""])[0].zfill(6) if qs.get("c") else ""
        try:
            with LOCK:
                self.route(path, code, qs, token, client)
        except Exception as e:
            log("异常 " + traceback.format_exc()[-400:])
            self._send(page("出错了", f'<div style="color:{RED}">{type(e).__name__}: {e}</div>', token), 500)

    def route(self, path, code, qs, token, client):
        if path == "/":
            self._send(page("我的持仓", position_rows(token), token))
        elif path == "/buy" and code:
            sell_u = f"/sell?c={code}&k={token}"
            p, isnew = P.add(code, sector=(qs.get("s") or [""])[0], signal_price=float((qs.get("p") or [0])[0] or 0) or None,
                             source=(qs.get("src") or ["共振邮件"])[0])
            log(f"{client} 买入 {p['code']} {p['name']} @ {p['buy_price']}" + ("" if isnew else "（已持有，更新）"))
            body = (f'<div style="font-size:17px;font-weight:700">已记下 {p["name"]} {p["code"]}</div>'
                    f'<div style="margin-top:8px;font-size:15px">买入价 <b>{p["buy_price"]}</b>'
                    f'<form action="/price" method="get" style="display:inline-block;margin-left:10px">'
                    f'<input type="hidden" name="c" value="{p["code"]}"><input type="hidden" name="k" value="{token}">'
                    f'<input name="v" type="number" step="0.01" placeholder="不对就改" style="width:110px;padding:7px;border:1px solid {LINE};border-radius:6px;font-size:14px">'
                    f'<button style="padding:7px 12px;margin-left:4px;border:1px solid {LINE};background:#fff;border-radius:6px;font-size:14px">改价</button></form></div>'
                    f'<div style="margin-top:10px;color:{GRAY};font-size:13px;line-height:1.6">'
                    f'明天 09:30 起每分钟盯这只，冲高、回落、无溢价、快到 10:00 都会给你发邮件。<br>卖掉之后回来点「我已卖出」，监控才会停。</div>'
                    f'<div style="margin-top:10px">{btn(L.stock_url(p["code"]), "看行情")}{btn(sell_u, "我已卖出", GREEN)}</div>')
            self._send(page("买入已记录", body, token))
        elif path == "/sell" and code:
            p = P.sell(code)
            if not p:
                self._send(page("没找到", "这只不在持仓里，可能已经标记过卖出了。", token))
                return
            log(f"{client} 卖出 {p['code']} {p['name']} @ {p['sold_price']} 盈亏 {p.get('pnl')}%")
            body = (f'<div style="font-size:17px;font-weight:700">{p["name"]} 已标记卖出</div>'
                    f'<div style="margin-top:8px;font-size:15px">买入 {p["buy_price"]} → 卖出 {p["sold_price"]}　{pct_html(p.get("pnl"))}</div>'
                    f'<div style="margin-top:8px;color:{GRAY};font-size:13px">这只的冲高监控已停止。</div>')
            self._send(page("已卖出", body, token))
        elif path == "/price" and code:
            v = (qs.get("v") or [""])[0]
            p = P.set_price(code, float(v)) if v else None
            self._send(page("买入价已更新" if p else "没改成", position_rows(token), token))
        elif path == "/del" and code:
            n = P.remove(code)
            log(f"{client} 删除 {code} x{n}")
            self._send(page("已删除" if n else "没找到", position_rows(token), token))
        else:
            self._send(page("我的持仓", position_rows(token), token))


def main():
    ap = argparse.ArgumentParser(description="持仓回执 Web 服务")
    ap.add_argument("--port", type=int, default=int(L.load_env().get("WEB_PORT") or 8085))
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.token = L.load_env().get("WEB_TOKEN", "")
    srv.daemon_threads = True
    log(f"启动 {a.host}:{a.port} token={'有' if srv.token else '无（建议在 .env 配 WEB_TOKEN）'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("停止")


if __name__ == "__main__":
    main()
