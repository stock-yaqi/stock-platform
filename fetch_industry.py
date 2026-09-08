#!/usr/bin/env python3
"""拉取全市场东财行业分类，保存到 mins/_meta/industry_em.json（{代码: {em, em1, csrc}}），已有的不重复拉。"""
import glob, json, os, time, requests
from concurrent.futures import ThreadPoolExecutor
HERE=os.path.dirname(os.path.abspath(__file__)); OUT=os.path.join(HERE,"mins","_meta","industry_em.json")
H={"User-Agent":"Mozilla/5.0","Referer":"https://emweb.securities.eastmoney.com/","Connection":"close"}
def fetch(code):
    pre="SH" if code.startswith(("6","9")) else "BJ" if code.startswith(("4","8")) else "SZ"
    for i in range(3):
        try:
            jb=requests.get(f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={pre}{code}",headers=H,proxies={"http":None,"https":None},timeout=12).json()["jbzl"][0]
            em=jb.get("EM2016") or ""; return code,{"em":em,"em1":em.split("-")[0],"csrc":jb.get("INDUSTRYCSRC1") or ""}
        except Exception: time.sleep(1+i)
    return code,None
if __name__=="__main__":
    done=json.load(open(OUT)) if os.path.exists(OUT) else {}
    codes=[os.path.basename(d) for d in glob.glob(os.path.join(HERE,"mins","[0-9]*")) if os.path.basename(d) not in done]
    print("待拉",len(codes),"已有",len(done),flush=True)
    with ThreadPoolExecutor(8) as ex:
        for n,(c,v) in enumerate(ex.map(fetch,codes),1):
            if v: done[c]=v
            if n%500==0: json.dump(done,open(OUT,"w"),ensure_ascii=False); print("进度",n,flush=True)
    json.dump(done,open(OUT,"w"),ensure_ascii=False); print("完成，共",len(done),"缺",sum(1 for c in codes if c not in done))
