#!/usr/bin/env python3
"""轻量监控（普适，--symbol 任意币）：价格 / OI / 费率 / 盘口 /（TON 币）链上大额转账。

数据源自动降级（重要）：
  - 优先币安 fapi（本机经代理；GitHub Actions 的美国 IP 会被 451 拒绝）；
  - 币安不可用时自动改用 **Gate 永续**（tickers + contract_stats + order_book，全球可用）；
  - 链上部分只在 profile 里配了 jetton 的 TON 币才跑（tonapi 直连，runner 也可用）。

用法:
    python3 monitor.py --symbol EVAA --out /tmp/monitor                    # 海外直连
    python3 monitor.py --symbol EVAA --out /tmp/monitor --proxy http://127.0.0.1:7890
    python3 monitor.py --symbol BTC --out /tmp/monitor --json

状态文件 <out>/state.json；首次只建基线；异常退出码 2。
"""
import argparse
import json
import os
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "coin_profiles.json")


def load_profile(raw_symbol):
    base = raw_symbol.upper().replace("_", "").replace("USDT", "")
    prof = {"symbol": base, "binance": base + "USDT", "gate": base + "_USDT",
            "network": None, "jetton": None}
    try:
        conf = json.load(open(PROFILES))
        for k, v in (conf.get(base) or {}).items():
            if v is not None:
                prof[k] = v
        for k, v in (conf.get("default") or {}).items():
            if prof.get(k) in (None, "") and v is not None:
                prof[k] = v.replace("{SYM}", base) if isinstance(v, str) else v
    except Exception:
        pass
    return prof


def get_json(url, proxy=None, timeout=25):
    if proxy:
        import requests
        s = requests.Session()
        s.headers.update(UA)
        s.proxies.update({"http": proxy, "https": proxy})
        return s.get(url, timeout=timeout).json()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EVAA")
    ap.add_argument("--out", default="/tmp/monitor")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--jetton", default="")
    ap.add_argument("--min-amount", type=float, default=0)
    a = ap.parse_args()
    prof = load_profile(a.symbol)
    sym, pair = prof["symbol"], prof["binance"]
    gate_contract = prof.get("gate") or (sym + "_USDT")
    os.makedirs(a.out, exist_ok=True)
    state_path = os.path.join(a.out, "state.json")
    old = json.load(open(state_path)) if os.path.exists(state_path) else {}
    alerts, snap = [], {"symbol": sym, "pair": pair}
    min_amount = a.min_amount or 3000

    def add(name, cur, key, pct_threshold):
        prev = old.get(key)
        # 换数据源时 OI/深度口径不同，跳过比较，避免误报
        if prev and old.get("source") == source:
            chg = (cur - prev) / prev * 100
            if abs(chg) >= pct_threshold:
                alerts.append(f"{name} {chg:+.1f}%（前值 {prev:,.2f} → {cur:,.2f}）")
        snap[key] = cur

    source = None
    # ---- 币安优先 ----
    try:
        source = "binance"
        px = get_json(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={pair}", a.proxy)
        p = float(px["price"])
        snap["price"] = p
        prev = old.get("price")
        if prev and abs(p - prev) / prev * 100 >= 5:
            alerts.append(f"价格 {p:,.4f}（较上次 {(p-prev)/prev*100:+.1f}%）")
        t24 = get_json(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={pair}", a.proxy)
        snap["price_24h_pct"] = float(t24["priceChangePercent"])
        oi = get_json(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={pair}&period=1h&limit=2", a.proxy)
        add("OI", float(oi[-1]["sumOpenInterestValue"]), "oi_usd", 10)
        pm = get_json(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={pair}", a.proxy)
        snap["funding_pct"] = round(float(pm["lastFundingRate"]) * 100, 4)
        if abs(snap["funding_pct"]) >= 0.1:
            alerts.append(f"资金费率异常 {snap['funding_pct']:+.3f}%/8h")
        d = get_json(f"https://fapi.binance.com/fapi/v1/depth?symbol={pair}&limit=100", a.proxy)
        bids, asks = d["bids"], d["asks"]
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        lo = mid * 0.99
        depth = sum(float(p) * float(q) for p, q in bids if float(p) >= lo)
        snap["depth_1pct_usd"] = round(depth)
        add("±1% 买盘深度", depth, "depth_1pct_usd", 30)
        source = "binance"
    except Exception as e:
        source = None
        alerts.append(f"币安不可用（{str(e)[:40]}），改用 Gate")

    # ---- Gate 降级 ----
    if source is None:
        try:
            source = "gate"
            t = get_json(f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={gate_contract}", a.proxy)[0]
            snap["price"] = float(t["last"])
            snap["price_24h_pct"] = float(t.get("change_percentage") or 0)
            prev = old.get("price")
            if prev and abs(snap["price"] - prev) / prev * 100 >= 5:
                alerts.append(f"价格 {snap['price']:,.4f}（较上次 {(snap['price']-prev)/prev*100:+.1f}%）")
            snap["funding_pct"] = round(float(t.get("funding_rate") or 0) * 100, 4)
            if abs(snap["funding_pct"]) >= 0.1:
                alerts.append(f"资金费率异常 {snap['funding_pct']:+.3f}%/8h")
            cs = get_json(f"https://api.gateio.ws/api/v4/futures/usdt/contract_stats"
                          f"?contract={gate_contract}&interval=1h&limit=1", a.proxy)[0]
            add("OI", float(cs["open_interest_usd"]), "oi_usd", 10)
            ob = get_json(f"https://api.gateio.ws/api/v4/futures/usdt/order_book"
                          f"?contract={gate_contract}&limit=100", a.proxy)
            bids = [[x["p"], x["s"]] for x in ob.get("bids", [])]
            mid = float(bids[0][0]) if bids else snap["price"]
            depth = sum(float(p) * float(q) for p, q in bids if float(p) >= mid * 0.99)
            snap["depth_1pct_usd"] = round(depth)
            add("±1% 买盘深度", depth, "depth_1pct_usd", 30)
            source = "gate"
        except Exception as e:
            alerts.append(f"Gate 也失败: {str(e)[:60]}")

    snap["source"] = source

    # ---- 链上（仅 TON 且配了 jetton）----
    jetton = a.jetton or prof.get("jetton")
    if jetton:
        try:
            h = get_json(f"https://tonapi.io/v2/jettons/{jetton}/holders?limit=100&offset=0", a.proxy)
            seen = set(old.get("event_ids", []))
            first_run = not old
            now = time.time()
            big = []
            for row in h.get("addresses", [])[:10]:
                owner = row["owner"]["address"]
                try:
                    d = get_json(f"https://tonapi.io/v2/accounts/{owner}/events?limit=50", a.proxy)
                except Exception:
                    continue
                for ev in d.get("events", []):
                    for act in ev.get("actions", []):
                        jt = act.get("JettonTransfer")
                        if not jt:
                            continue
                        amt = int(jt["amount"]) / 1e9
                        if amt < min_amount:
                            continue
                        eid = ev.get("event_id")
                        if eid in seen:
                            continue
                        ts = ev.get("timestamp") or 0
                        if now - ts > 48 * 3600:
                            continue
                        seen.add(eid)
                        big.append({"time": ts, "amount": amt,
                                    "from": jt["sender"]["address"], "to": jt["recipient"]["address"]})
                time.sleep(0.8)
            snap["event_ids"] = list(seen)[-2000:]
            snap["holders"] = len(h.get("addresses", []))
            if first_run:
                big = []
            if big:
                alerts.append(f"链上新增大额转账 {len(big)} 笔（≥{min_amount:,.0f} {sym}）")
                snap["big_transfers"] = big[:10]
        except Exception as e:
            alerts.append(f"链上失败: {str(e)[:60]}")

    snap["updated"] = int(time.time())
    with open(state_path, "w") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)

    result = {"symbol": sym, "updated": snap["updated"], "alerts": alerts, "snapshot": snap}
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"[{time.strftime('%m-%d %H:%M')}] {sym} 监控（{pair}）")
        print(f"  源 {snap.get('source')} · 价格 {snap.get('price')} · 24h {snap.get('price_24h_pct')}% · "
              f"OI ${snap.get('oi_usd',0)/1e6:.1f}M · 费率 {snap.get('funding_pct')}%/8h · "
              f"±1% 深度 ${snap.get('depth_1pct_usd',0)/1e3:.1f}K")
        if alerts:
            print("  ！异常：")
            for x in alerts:
                print("   -", x)
        else:
            print("  无异常")
    return 2 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
