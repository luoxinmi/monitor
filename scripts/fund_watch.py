#!/usr/bin/env python3
"""币安保险基金 / OI 采样（供私有仓库 luoxinmi/fund-watch 的 Actions 运行）。

为什么用 www.binance.com
------------------------
GitHub Actions runner 在美国，直连 `fapi.binance.com` 返回 **451**；
但 `www.binance.com/fapi/<path>` 是网页端透传，实测 **200 且返回真实 JSON**
（insuranceBalance / openInterest / klines / premiumIndex / futures/data 全部可用）。
所以云端采样不需要代理，换个域名即可。

输出：data/fund/YYYYMM.jsonl，每次采样一行（fund-watch 仓库 cron 5 分钟；monitor 的 sample step 每小时一次），字段：
  {"ts", "sym", "group_fund_usdt", "group_syms", "oi", "price"}
同一行还带 Gate 日级余额（Gate 全球可达）。
"""
import argparse
import json
import os
import time
import urllib.request

BASE = "https://www.binance.com"


def _get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0 Chrome/126"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def find_group(ins, sym):
    want = {f"{sym}USDT", f"{sym}USDC"}
    for p in ("100", "1000", "10000", "100000", "1000000"):
        want.add(f"{p}{sym}USDT")
        want.add(f"{p}{sym}USDC")
    return next((g for g in ins if any(x in want for x in (g.get("symbols") or []))), None)


def gate_insurance():
    try:
        req = urllib.request.Request("https://api.gateio.ws/api/v4/futures/usdt/insurance",
                                     headers={"User-Agent": "fund-watch"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        return float(d[-1]["b"]) if d else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="EVAA,ETH,PEPE,BONK")
    ap.add_argument("--out", default="data/fund")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ts = int(time.time())
    rows = []
    try:
        ins = _get("/fapi/v1/insuranceBalance?limit=200")
    except Exception as e:
        print("insuranceBalance 失败:", e)
        return 1
    gate = gate_insurance()
    for sym in [s.strip().upper() for s in a.symbols.split(",") if s.strip()]:
        grp = find_group(ins, sym)
        fund = None
        syms = None
        if grp:
            fund = sum(float(x["marginBalance"]) for x in grp.get("assets", [])
                       if str(x.get("asset", "")).upper() in ("USDT", "USDC"))
            syms = grp.get("symbols")
        oi = price = None
        try:
            oi = float(_get(f"/fapi/v1/openInterest?symbol={sym}USDT")["openInterest"])
            price = float(_get(f"/fapi/v1/ticker/price?symbol={sym}USDT")["price"])
        except Exception:
            pass
        rows.append({"ts": ts, "sym": sym, "group_fund_usdt": fund, "group_syms": syms,
                     "oi": oi, "price": price, "gate_insurance": gate})
        print(f"{sym}: 组 ${(fund or 0)/1e6:.2f}M · OI {oi} · ${price}")
    path = os.path.join(a.out, time.strftime("%Y%m.jsonl", time.gmtime(ts)))
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("写入", path, len(rows), "行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
