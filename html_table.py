"""筹码集中表的本地 HTML 生成（自带数据，离线可开，可排序筛选）。scan_chips.py 调用。"""
import json

TEMPLATE = r'''<title>筹码集中筛选表</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@700&family=Noto+Sans+SC:wght@400;500;700&display=swap">
<style>
:root{--bg:#F6F5F1;--surface:#FFFFFF;--ink:#1F2429;--muted:#6B6F73;--line:#DDDBD4;--accent:#A8322B;--up:#D23F31;--down:#1F9D55;--hover:#F0EEE8;--chip:#EDEBE4;}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#17191C;--surface:#1F2226;--ink:#E9E6DF;--muted:#9A9C9F;--line:#33363B;--accent:#E06A60;--up:#E06A60;--down:#4FB37A;--hover:#262A2F;--chip:#2A2E33;}}
:root[data-theme="dark"]{--bg:#17191C;--surface:#1F2226;--ink:#E9E6DF;--muted:#9A9C9F;--line:#33363B;--accent:#E06A60;--up:#E06A60;--down:#4FB37A;--hover:#262A2F;--chip:#2A2E33;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans SC","PingFang SC","Hiragino Sans GB",sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
h1{font-family:"Noto Serif SC",serif;font-size:28px;margin:0;letter-spacing:.02em}.sub{color:var(--muted);font-size:13px;margin-top:6px}
header{border-bottom:2px solid var(--ink);padding-bottom:14px}
.rules{margin:20px 0;background:var(--surface);border:1px solid var(--line)}
.rules table{width:100%;border-collapse:collapse;font-size:13px}.rules th,.rules td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.rules th{font-weight:500;color:var(--muted);font-size:12px;letter-spacing:.06em}.rules td b{color:var(--accent)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0}
.chip{border:1px solid var(--line);background:var(--chip);padding:5px 11px;border-radius:999px;cursor:pointer;font:inherit;color:var(--ink);font-size:13px}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
input[type=search],select{padding:6px 10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);font:inherit;border-radius:3px}
.count{color:var(--muted);font-size:13px;margin-left:auto}
.tw{overflow-x:auto;background:var(--surface);border:1px solid var(--line)}
table.data{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;white-space:nowrap}
table.data th,table.data td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
table.data th:nth-child(-n+4),table.data td:nth-child(-n+4){text-align:left}
table.data th{position:sticky;top:0;background:var(--surface);color:var(--muted);font-weight:500;font-size:12px;cursor:pointer;user-select:none;letter-spacing:.04em}
table.data th.on{color:var(--ink)}table.data tr:hover td{background:var(--hover)}
.score{display:inline-block;min-width:22px;text-align:center;border-radius:3px;padding:1px 6px;font-weight:700;color:#fff}
.s4{background:#A8322B}.s3{background:#B8742A}.s2{background:#6B7B8C}.s1{background:#9A9C9F}
.ok{color:var(--down);font-weight:700}.no{color:var(--line)}
.pos{color:var(--up)}.neg{color:var(--down)}
h2{font-size:17px;margin:32px 0 8px;font-family:"Noto Serif SC",serif}
.note{color:var(--muted);font-size:12px;max-width:72ch;line-height:1.7}
@media (max-width:640px){.bar .count{margin-left:0}}
</style>
<div class="wrap">
<header><h1>筹码集中筛选表</h1><div class="sub" id="sub"></div></header>

<div class="rules"><table>
<tr><th style="width:56px">分项</th><th style="width:260px">条件</th><th>依据（东财 F10 户数 2024-03 至 2026-09，5211 只，相对全市场同期超额）</th></tr>
<tr><td><b>①</b></td><td>最新一期股东户数环比下降 ≥ 10%</td><td>户数降 ≥10%：下一期 <b>+2.04%</b>，披露后再下一期 +1.14%（93 期）；降 ≥15%：+2.82% / +1.53%</td></tr>
<tr><td><b>②</b></td><td>连续两期户数下降 ≥ 5%</td><td>下一期 <b>+1.71%</b>，16 个期数里 11 个为正；说明不是一次性的减少</td></tr>
<tr><td><b>③</b></td><td>前十大流通股东持股占比比上期上升</td><td>户数降 ≥5% 且前十大上升：下一期 <b>+2.22%</b>（8 期里 7 期为正）；大股东和机构在增持</td></tr>
<tr><td><b>④</b></td><td>低位缩量：距 40 日最低 ≤10%、20 日涨幅为负、5 日均成交额 &lt; 1.2 倍 20 日均额</td><td>价格还没走、量还没起，筹码集中在低位才有意义；低位缩量在分钟数据回测里 20 日超额 +4.2%</td></tr>
<tr><td><b>回避</b></td><td>最新一期户数环比上升 ≥ 20%</td><td>散户涌入：下一期 <b>-2.50%</b>，18 期里只有 8 期为正；再下一期 -0.60%</td></tr>
</table></div>
<p class="note">怎么用：先看 4 分和 3 分，再按行业和成交额挑；户数是季度（部分公司月度）数据，滞后 1 到 2 个月，10 月底三季报后会刷新，属于中期筹码线索，不是买卖时点。表头可点击排序，"入选项"四列 ✓ 表示满足该分项。</p>

<div class="bar">
<span style="color:var(--muted);font-size:12px">得分</span>
<button class="chip sc" data-s="4" aria-pressed="true">4 分 <span id="n4"></span></button>
<button class="chip sc" data-s="3" aria-pressed="true">3 分 <span id="n3"></span></button>
<button class="chip sc" data-s="2" aria-pressed="false">2 分 <span id="n2"></span></button>
<button class="chip sc" data-s="1" aria-pressed="false">1 分 <span id="n1"></span></button>
<select id="ind"><option value="">全部行业</option></select>
<input type="search" id="q" placeholder="搜代码或名称">
<span class="count" id="cnt"></span>
</div>
<div class="tw"><table class="data" id="t"><thead><tr>
<th data-k="s" class="on">得分</th><th data-k="c">代码</th><th data-k="n">名称</th><th data-k="i">行业</th>
<th data-k="s1">①降10%</th><th data-k="s2">②连降</th><th data-k="s3">③前十大↑</th><th data-k="s4">④低位缩量</th>
<th data-k="g">户数环比</th><th data-k="gp">上期环比</th><th data-k="h">户数</th><th data-k="t">前十大%</th><th data-k="td">前十大变化</th>
<th data-k="p">收盘</th><th data-k="dl">距40日低</th><th data-k="r20">20日涨幅</th><th data-k="vr">量比</th><th data-k="a">20日均额亿</th><th data-k="f">集中度</th>
</tr></thead><tbody></tbody></table></div>

<h2>户数大增回避（≥ 20%）</h2>
<p class="note">这些公司最新一期股东户数比上期增加 20% 以上，说明筹码在从少数人手里散到多数人手里，历史上下一期平均跑输大盘 2.5 个百分点。前 60 只按增幅排序，完整名单在 CSV 里。</p>
<div class="tw"><table class="data" id="ta"><thead><tr><th>代码</th><th>名称</th><th>行业</th><th style="text-align:right">户数环比</th><th style="text-align:right">上期环比</th><th style="text-align:right">户数</th><th style="text-align:left">期末</th></tr></thead><tbody></tbody></table></div>
</div>
<script>
const D=__DATA__;
const cand=D.cand, avoid=D.avoid;
const pct=(v,d=1)=>v==null?'<span style="color:var(--muted)">—</span>':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${v.toFixed(d)}%</span>`;
const tick=b=>b?'<span class="ok">✓</span>':'<span class="no">·</span>';
let active=new Set(['4','3']), ind='', q='', sortK='s', dir=-1;
document.getElementById('sub').textContent=`股东户数最新一期 ${D.meta.period} · 股价与成交数据截至 ${D.meta.day} · 股票池 ${D.meta.pool} · 候选 ${cand.length} 只 · 户数大增回避 ${avoid.length} 只`;
for(const s of ['4','3','2','1']) document.getElementById('n'+s).textContent='('+cand.filter(r=>String(r.s)===s).length+')';
const inds=[...new Set(cand.map(r=>r.i).filter(Boolean))].sort(); const sel=document.getElementById('ind');
for(const i of inds){const o=document.createElement('option');o.value=i;o.textContent=i;sel.appendChild(o);}
document.querySelectorAll('.sc').forEach(b=>b.onclick=()=>{const s=b.dataset.s;if(active.has(s))active.delete(s);else active.add(s);b.setAttribute('aria-pressed',String(active.has(s)));render();});
sel.onchange=e=>{ind=e.target.value;render();};document.getElementById('q').oninput=e=>{q=e.target.value.trim().toLowerCase();render();};
document.querySelectorAll('#t th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(sortK===k)dir*=-1;else{sortK=k;dir=(k==='n'||k==='c'||k==='i'||k==='f')?1:-1;}document.querySelectorAll('#t th').forEach(x=>x.classList.toggle('on',x===th));render();});
function render(){
 let rows=cand.filter(r=>active.has(String(r.s))&&(!ind||r.i===ind)&&(!q||r.c.includes(q)||r.n.toLowerCase().includes(q)));
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='boolean'){x=+x;y=+y;}if(x==null)return 1;if(y==null)return -1;if(typeof x==='string')return x.localeCompare(y,'zh')*dir;return (x-y)*dir;});
 document.getElementById('cnt').textContent=rows.length+' 只';
 document.querySelector('#t tbody').innerHTML=rows.map(r=>`<tr><td><span class="score s${r.s}">${r.s}</span></td><td>${r.c}</td><td><b>${r.n}</b></td><td>${r.i||''}</td>
 <td style="text-align:center">${tick(r.s1)}</td><td style="text-align:center">${tick(r.s2)}</td><td style="text-align:center">${tick(r.s3)}</td><td style="text-align:center">${tick(r.s4)}</td>
 <td>${pct(r.g)}</td><td>${pct(r.gp)}</td><td>${r.h.toLocaleString()}</td><td>${r.t==null?'—':r.t.toFixed(1)}</td><td>${pct(r.td)}</td>
 <td>${r.p==null?'—':r.p}</td><td>${pct(r.dl)}</td><td>${pct(r.r20)}</td><td>${r.vr==null?'—':r.vr.toFixed(2)}</td><td>${r.a==null?'—':r.a.toFixed(1)}</td><td>${r.f||''}</td></tr>`).join('');
}
document.querySelector('#ta tbody').innerHTML=avoid.slice().sort((a,b)=>b.g-a.g).slice(0,60).map(r=>`<tr><td>${r.c}</td><td><b>${r.n}</b></td><td>${r.i||''}</td><td>${pct(r.g)}</td><td>${pct(r.gp)}</td><td>${r.h.toLocaleString()}</td><td style="text-align:left">${r.d}</td></tr>`).join('');
render();
</script>'''


def render(cand_rows, avoid_rows, meta):
    return TEMPLATE.replace("__DATA__", json.dumps({"cand": cand_rows, "avoid": avoid_rows, "meta": meta}, ensure_ascii=False))
