#!/usr/bin/env python3
"""全市场扫描（GitHub Actions 用）：**每个代币一个文件夹**，覆盖全平台。

四件事（对应云端职责）：
  A. 各平台基本信息：币安 / OKX / Bybit / Gate / Bitget / Hyperliquid 的
     价格、24h 涨跌、24h 成交额、OI（USD + 币数）、资金费率、最大杠杆、上市时间
  B. 基金变化：币安保险基金**分组池**（一组共用 F_g）+ Gate 全站保险基金
  C. 每币兑现缺口 gap_ratio（图 16 防妖币）：30 天日级 OI + 日线 LIFO 逐日算，
     给出当前值、30 天最大值与时点（币安机制：每个代币归到一个小组，组内共用基金池）
  D. 组级历史最大缺口：组成员 × 30 天序列取最大，供本地图 16 直接读

输出（按币名建文件夹，data/<SYM>/）：
  venues.json      各平台基本信息（每轮覆盖；只有该币真的在的所）
  gap.json         当前 gap + 30 天最大 + 所属组 / 组基金 / 组历史最大缺口
  gap_daily.jsonl  逐日 gap（首轮补齐 30 天，之后每天追加一行）
另外全局：
  data/groups/<gid>.json  组基金、成员、组内历史最大缺口
  data/groups/index.json  组清单
  data/_meta/scan.json    本轮扫描元数据（平台可达性 / 币数 / 耗时）
  data/_meta/market.json  全市场一币一行，本地选币用

云端直连：币安走 www.binance.com/fapi 透传（fapi 在 runner 是 451）；
OKX / Bybit / Gate / Bitget / Hyperliquid 直连。本地跑加 --proxy。
价格一律原始合约价（1000PEPE 不折算），venues 里带该所合约名，避免混口径。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
BN_HOSTS = ("https://fapi.binance.com", "https://www.binance.com")
MM = 0.004
LEVS = ((10, 0.30), (8, 0.15), (5, 0.30), (3, 0.15), (2, 0.10))


# ---------------------------------------------------------------- 网络
class Client:
    _bn_host = None          # 记住可用的币安域名，避免每请求先撞一次 451

    def __init__(self, proxy=None, timeout=15):
        self.proxy = proxy
        self.timeout = timeout
        self.opener = None
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    def get_json(self, url, timeout=None, retries=3, method="GET", data=None, headers=None):
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers={**UA, **(headers or {})},
                                             method=method, data=data)
                t = timeout or self.timeout
                opener = self.opener.open if self.opener else urllib.request.urlopen
                with opener(req, timeout=t) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (418, 429):
                    time.sleep(2 + i * 2)
                    continue
                if e.code >= 500:
                    time.sleep(1 + i)
                    continue
                raise
            except Exception as e:      # 超时 / 连接重置 / SSL EOF
                last = e
                time.sleep(0.6 + i * 0.8)
        raise last

    def bn(self, path):
        """币安 fapi：直连失败自动换 www 透传域名；成功后记住，后续直接用它。"""
        hosts = list(BN_HOSTS)
        if Client._bn_host:
            hosts.remove(Client._bn_host)
            hosts.insert(0, Client._bn_host)
        last = None
        for host in hosts:
            try:
                d = self.get_json(host + path, retries=2)
                Client._bn_host = host
                return d
            except Exception as e:
                last = e
        raise last


# ---------------------------------------------------------------- 工具
def _num(x, default=None):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def _base(sym):
    """合约名 → 币名：BTCUSDT→BTC、BTC_USDT→BTC、BTC-USDT-SWAP→BTC、BTCUSDT_... 已先处理。"""
    s = str(sym).upper()
    s = re.sub(r"[-_].*$", "", s)          # 去掉 -USDT / _USDT 后缀（连字符/下划线开头）
    s = re.sub(r"USDT$", "", s)            # BTCUSDT
    return s


# ---------------------------------------------------------------- 平台适配器
def scan_binance(c):
    ex = c.bn("/fapi/v1/exchangeInfo")
    t24 = c.bn("/fapi/v1/ticker/24hr")
    pm = c.bn("/fapi/v1/premiumIndex")
    meta = {}
    for s in ex.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        meta[s["symbol"]] = {"base": s["baseAsset"], "onboard": int(s.get("onboardDate") or 0)}
    tick = {x["symbol"]: x for x in t24 if isinstance(x, dict)}
    prem = {x["symbol"]: x for x in pm if isinstance(x, dict)}
    rows = {}
    for symbol, m in meta.items():
        t = tick.get(symbol) or {}
        p = _num(t.get("lastPrice")) or _num(prem.get(symbol, {}).get("markPrice"))
        if not p:
            continue
        rows[m["base"]] = {
            "sym": symbol, "p": p,
            "pc": _num(t.get("priceChangePercent")),
            "v": _num(t.get("quoteVolume")),
            "fr": _num(prem.get(symbol, {}).get("lastFundingRate")),
            "lev": None, "onboard": m["onboard"],
        }
    return rows


def scan_okx(c):
    tk = c.get_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
    oi = c.get_json("https://www.okx.com/api/v5/public/open-interest?instType=SWAP")
    inst = c.get_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    lev = {}
    for x in inst.get("data") or []:
        if str(x.get("instId", "")).endswith("-USDT-SWAP"):
            lev[x["instId"]] = _num(x.get("lever"))
    oimap = {x["instId"]: _num(x.get("oiUsd")) for x in (oi.get("data") or [])}
    rows = {}
    for t in tk.get("data") or []:
        iid = t.get("instId") or ""
        if not iid.endswith("-USDT-SWAP") and not iid.endswith("-USD-SWAP"):
            continue
        base = iid.split("-")[0]
        row = {
            "sym": iid, "p": _num(t.get("last")),
            "pc": round((_num(t.get("last")) / _num(t.get("open24h")) - 1) * 100, 4)
            if _num(t.get("open24h")) else None,
            "v": _num(t.get("volCcy24h")),
            "oi": oimap.get(iid), "oi_coin": None,
            "fr": None, "lev": lev.get(iid), "onboard": None,
        }
        if row["p"] and (iid.endswith("-USDT-SWAP")):
            rows[base] = row
    return rows


def scan_bybit(c):
    # v5 tickers 的 deliveryTime 对永续是字符串 "0"（truthy），不能直接当空判断；
    # 用 instruments-info 拿 baseCoin / quoteCoin / contractType 才准（还有 XXXPERP 新命名）。
    d = c.get_json("https://api.bybit.com/v5/market/tickers?category=linear")
    inst = {}
    try:
        cursor = ""
        for _ in range(10):
            u = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
            if cursor:
                u += f"&cursor={cursor}"
            ii = c.get_json(u)
            res = (ii.get("result") or {})
            for x in res.get("list") or []:
                if str(x.get("quoteCoin", "")).upper() == "USDT" and \
                        str(x.get("contractType", "")).upper() in ("LINEARPERPETUAL", "PERPETUAL", ""):
                    inst[x.get("symbol")] = x.get("baseCoin") or str(x.get("symbol")).replace("PERP", "").replace("USDT", "")
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
    except Exception:
        inst = {}
    rows = {}
    for t in (d.get("result") or {}).get("list") or []:
        sym = t.get("symbol") or ""
        if sym in inst:
            base = inst[sym]
        elif sym.endswith("USDT"):
            base = sym[:-4]
        else:
            continue
        if not base:
            continue
        rows[base] = {
            "sym": sym, "p": _num(t.get("lastPrice")),
            "pc": round((_num(t.get("price24hPcnt")) or 0) * 100, 4),
            "v": _num(t.get("turnover24h")),
            "oi": _num(t.get("openInterestValue")), "oi_coin": _num(t.get("openInterest")),
            "fr": _num(t.get("fundingRate")), "lev": None, "onboard": None,
        }
    return rows


def scan_gate(c):
    tk = c.get_json("https://api.gateio.ws/api/v4/futures/usdt/tickers")
    ct = c.get_json("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    lev = {}
    for x in ct:
        if str(x.get("name", "")).endswith("_USDT"):
            lev[x["name"]] = _num(x.get("leverage_max"))
    rows = {}
    for t in tk:
        name = t.get("contract") or ""
        if not name.endswith("_USDT"):
            continue
        base = name[:-5]
        qm = _num(t.get("quanto_multiplier")) or 1.0
        oi_coin = _num(t.get("total_size"))  # 张数
        p = _num(t.get("last"))
        rows[base] = {
            "sym": name, "p": p,
            "pc": _num(t.get("change_percentage")),
            "v": _num(t.get("volume_24h_settle")) or _num(t.get("volume_24h_quote")),
            "oi": (oi_coin * qm * p) if (oi_coin is not None and p) else None,
            "oi_coin": (oi_coin * qm) if oi_coin is not None else None,
            "fr": _num(t.get("funding_rate")), "lev": lev.get(name), "onboard": None,
        }
    return rows


def scan_bitget(c):
    d = c.get_json("https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
    rows = {}
    for t in d.get("data") or []:
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        p = _num(t.get("lastPr"))
        hold = _num(t.get("holdingAmount"))
        rows[base] = {
            "sym": sym, "p": p,
            "pc": round((_num(t.get("change24h")) or 0) * 100, 4),
            "v": _num(t.get("usdtVolume")),
            "oi": (hold * p) if (hold is not None and p) else None,
            "oi_coin": hold,
            "fr": _num(t.get("fundingRate")), "lev": None, "onboard": None,
        }
    return rows


def scan_hyperliquid(c):
    d = c.get_json("https://api.hyperliquid.xyz/info", method="POST",
                   data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                   headers={"Content-Type": "application/json"})
    if not isinstance(d, list) or len(d) < 2:
        return {}
    uni, ctxs = d[0].get("universe") or [], d[1]
    rows = {}
    for u, x in zip(uni, ctxs):
        name = u.get("name") or ""
        mpx = _num(x.get("markPx")) or _num(x.get("midPx"))
        oi_coin = _num(x.get("openInterest"))
        if not name or not mpx:
            continue
        rows[name] = {
            "sym": name, "p": mpx,
            "pc": round((mpx / _num(x.get("prevDayPx")) - 1) * 100, 4) if _num(x.get("prevDayPx")) else None,
            "v": _num(x.get("dayNtlVlm")),
            "oi": (oi_coin * mpx) if oi_coin is not None else None,
            "oi_coin": oi_coin,
            "fr": _num(x.get("funding")),
            "lev": _num(u.get("maxLeverage")), "onboard": None,
        }
    return rows


VENUES = [
    ("binance", scan_binance),
    ("okx", scan_okx),
    ("bybit", scan_bybit),
    ("gate", scan_gate),
    ("bitget", scan_bitget),
    ("hyperliquid", scan_hyperliquid),
]


# ---------------------------------------------------------------- 兑现缺口（图 16）
def _layers(daily, base_days=30):
    """LIFO 成本分层：底仓 = 窗口起点 OI；正增量入层（成本=当日均价的近似 (o+h+l+c)/4），
    负增量从最新层往底仓扣。与本地 estimate.py / gap_ratio.py 同源。"""
    base_idx = max(0, len(daily) - (base_days + 1))
    base = daily[base_idx]
    layers = [[base[0], base[3], base[1]]]      # [day, cost, notional]
    for i in range(base_idx + 1, len(daily)):
        delta = daily[i][1] - daily[i - 1][1]
        if delta > 0:
            o, h, l, cl = daily[i][2], daily[i][3], daily[i][4], daily[i][5]
            layers.append([daily[i][0], (o + h + l + cl) / 4, delta])
        elif delta < 0:
            cut = -delta
            while cut > 1e-9 and layers:
                last = layers[-1]
                take = min(last[2], cut)
                last[2] -= take
                cut -= take
                if last[2] <= 1e-9:
                    layers.pop()
    return [x for x in layers if x[2] > 1e-9]


def compute_gap(daily, cur, long_share=0.5):
    if not daily or not cur or cur <= 0:
        return None
    layers = _layers(daily)
    if not layers:
        return None
    total = sum(x[2] for x in layers)
    long_pnl = sum(amt * (1 - cost / cur) for _, cost, amt in layers)
    short_alive = 0.0
    for _, cost, amt in layers:
        for lev, w in LEVS:
            lp = cost * (1 + 1 / lev - MM)
            if lp > cur:
                short_alive += amt * (1 - long_share) * w
    if short_alive <= 1e-9:
        return {"gap_ratio": None, "long_pnl_usd": round(long_pnl, 2),
                "short_alive_usd": 0.0, "total_oi_usd": round(total, 2), "layers": len(layers)}
    return {
        "gap_ratio": round(long_pnl / short_alive, 3),
        "long_pnl_usd": round(long_pnl, 2),
        "short_alive_usd": round(short_alive, 2),
        "total_oi_usd": round(total, 2),
        "layers": len(layers),
    }


def window_returns(c, pair, windows=(8, 30, 60, 240)):
    """1m K 线算各时间窗口涨幅（%）：8m / 30m / 1h / 4h。

    用交易所 K 线而不是采样历史：8 分钟档一轮只隔 8 分钟，本地采样根本攒不出 30m/1h/4h；
    1m×240 根一次请求覆盖 4 小时全部窗口。
    """
    kl = c.bn(f"/fapi/v1/klines?symbol={pair}&interval=1m&limit=240")
    closes = []
    for k in kl or []:
        try:
            closes.append(float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    if len(closes) < 10:
        return None
    cur = closes[-1]
    out = {}
    for w in windows:
        if len(closes) > w:
            p0 = closes[-1 - w]
            if p0 > 0:
                out[f"{w}m"] = round((cur / p0 - 1) * 100, 3)
    return out or None


def short_ladder(rows, long_share=0.5):
    """空头清算阶梯 [(强平价, 名义)]，升序——短时爆拉要按「哪些空头会被扫」逐档算。"""
    ladder = []
    for _, cost, amt in _layers(rows):
        for lev, w in LEVS:
            lp = cost * (1 + 1 / lev - MM)
            if lp > 0:
                ladder.append((lp, amt * (1 - long_share) * w))
    ladder.sort()
    return ladder


def squeeze_windows(ladder, cur, rets_pct, fund):
    """各时间窗口的「妖币爆拉缺口」：窗口内涨幅扫掉的空头名义 N 与穿仓损失 L，
    相对组基金池 F_g 就是 uN / uL（图 16 的 ②③ 层按时间尺度拆开）。

    损失口径：价格越过强平价的幅度 − MMR（穿仓率），只取正。
    """
    if not ladder or not cur or cur <= 0:
        return []
    total_alive = sum(a for _, a in ladder)
    out = []
    for w, z in (rets_pct or {}).items():
        if z is None or z <= 0:
            continue
        p0 = cur / (1 + z / 100.0)
        dead = [(lp, amt) for lp, amt in ladder if p0 <= lp <= cur]
        N = sum(amt for _, amt in dead)
        L = sum(amt * max(0.0, (cur - lp) / lp - MM) for lp, amt in dead)
        out.append({
            "w": w, "ret_pct": z, "from": round(p0, 10),
            "N": round(N, 2), "L": round(L, 2),
            "uN": round(N / fund, 4) if fund else None,
            "uL": round(L / fund, 4) if fund else None,
            "alive_short": round(total_alive - N, 2),
        })
    return out


def _squeeze_hit(rets):
    """爆拉阈值：8m 3% / 30m 6% / 1h 10% / 4h 20%（只看向上，向下是另一套机制）。"""
    thr = {"8m": 3.0, "30m": 6.0, "60m": 10.0, "240m": 20.0}
    hits = []
    for w, z in (rets or {}).items():
        if z is not None and z >= thr.get(w, 999):
            hits.append((w, z))
    return hits


def gate_daily_rows(c, gate_pair, days):
    """Gate 日级 OI（USD）+ mark_price：长窗口 gap 的数据源（币安 openInterestHist 只有 ~30 天）。"""
    d = c.get_json("https://api.gateio.ws/api/v4/futures/usdt/contract_stats"
                   f"?contract={gate_pair}&interval=1d&limit={min(days, 1000)}")
    rows = []
    for x in d or []:
        try:
            oi = _num(x.get("open_interest_usd")) or 0.0
            mpx = _num(x.get("mark_price")) or 0.0
            t = int(x.get("time") or 0)
            if oi <= 0 or mpx <= 0 or not t:
                continue
            rows.append((t // 86400, oi, mpx, mpx, mpx, mpx))
        except (TypeError, ValueError):
            continue
    rows.sort()
    return rows


def long_window_gap(c, base, gate_pair, days, cur=None):
    """长窗口（默认 180 天）gap：Gate 日级 OI + mark_price，LIFO 同口径。

    口径注意：Gate OI 是单所（约币安 60%），绝对金额偏小；但缺口倍数（多头浮盈/存活空单）形状可比。
    """
    rows = gate_daily_rows(c, gate_pair, days)
    if len(rows) < 5:
        return None
    cur = cur or rows[-1][5]
    hist = []
    for i in range(1, len(rows)):
        sub = rows[max(0, i - days):i + 1]
        g = compute_gap(sub, sub[-1][5], 0.5)
        if g and g.get("gap_ratio") is not None:
            hist.append({"d": time.strftime("%Y-%m-%d", time.gmtime(rows[i][0] * 86400)),
                         "gap": g["gap_ratio"], "lp": g["long_pnl_usd"], "sa": g["short_alive_usd"]})
    if not hist:
        return None
    best = max(hist, key=lambda x: x["gap"])
    cur_gap = compute_gap(rows, cur, 0.5)
    return {"window_days": len(rows), "gap": cur_gap, "max_gap": best,
            "seq": len(hist), "method": "gate_daily_oi_mark_price"}


def daily_rows(c, pair, days=30):
    """币安日级 OI 名义 + 日线 OHLC 对齐 → [(day, oi_usd, o, h, l, c)]。

    注意 openInterestHist 只保留最近约 30 天，1d 最多 30 条——云端窗口硬上限。
    """
    oi = c.bn(f"/futures/data/openInterestHist?symbol={pair}&period=1d&limit={days}")
    kl = c.bn(f"/fapi/v1/klines?symbol={pair}&interval=1d&limit={days}")
    if not isinstance(oi, list) or not isinstance(kl, list) or not oi or not kl:
        return []
    kmap = {}
    for k in kl:
        try:
            kmap[int(k[0]) // 86400000] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    out = []
    for x in oi:
        try:
            day = int(x["timestamp"]) // 86400000
            if day not in kmap:
                continue
            o, h, l, cl = kmap[day]
            out.append((day, float(x["sumOpenInterestValue"]), o, h, l, cl))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def gap_series(rows, long_share=0.5, base_days=30):
    """逐日 gap：对每个收盘日用截至当日的窗口重算，得到 30 天序列（含当日）。"""
    hist = []
    for i in range(1, len(rows)):
        sub = rows[max(0, i - base_days):i + 1]
        g = compute_gap(sub, sub[-1][5], long_share)
        if not g or g.get("gap_ratio") is None:
            continue
        hist.append({"d": time.strftime("%Y-%m-%d", time.gmtime(rows[i][0] * 86400)),
                     "gap": g["gap_ratio"], "lp": g["long_pnl_usd"], "sa": g["short_alive_usd"]})
    return hist


# ---------------------------------------------------------------- 保险基金分组
def binance_groups(c):
    ins = c.bn("/fapi/v1/insuranceBalance?limit=200")
    groups = []
    for i, g in enumerate(ins or []):
        fund = 0.0
        for a in g.get("assets") or []:
            if str(a.get("asset", "")).upper() in ("USDT", "USDC"):
                fund += _num(a.get("marginBalance")) or 0.0
        groups.append({"gid": f"g{i:03d}", "fund_usd": round(fund, 2),
                       "symbols": list(g.get("symbols") or []),
                       "updated": int(time.time())})
    return groups


def gate_insurance(c):
    try:
        d = c.get_json("https://api.gateio.ws/api/v4/futures/usdt/insurance")
        return _num(d[-1].get("b")) if d else None
    except Exception:
        return None


# ---------------------------------------------------------------- 主流程
_PROFILES = None


def _load_profiles():
    """coin_profiles.json：链上归属（network/jetton）等静态配置。"""
    global _PROFILES
    if _PROFILES is not None:
        return _PROFILES
    for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "coin_profiles.json"),
                 os.path.join(os.getcwd(), "scripts", "coin_profiles.json")):
        try:
            with open(cand) as f:
                _PROFILES = {str(k).upper(): v for k, v in json.load(f).items() if isinstance(v, dict)}
                return _PROFILES
        except Exception:
            continue
    _PROFILES = {}
    return _PROFILES


def _maybe_fund_series(out, groups, now, month, meta_dir, force=False):
    """组基金时序：每小时最多写一次（防止 8/30 分钟档把文件撑爆）。"""
    if not groups:
        return
    last = (_read_json(os.path.join(meta_dir, "fund.json"), {}) or {}).get("ts_fund") or 0
    if not force and now - int(last) < 55 * 60:
        return
    rows = [{"ts": now, "gid": g["gid"], "fund": g["fund_usd"], "n": len(g["symbols"])}
            for g in groups]
    _append_jsonl(os.path.join(out, "fund", f"groups_{month}.jsonl"), rows, key=None)
    fd = _read_json(os.path.join(meta_dir, "fund.json"), {}) or {}
    fd["ts_fund"] = now
    _write_json(os.path.join(meta_dir, "fund.json"), fd)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))


def _append_jsonl(path, rows, key=None):
    """追加 jsonl，且按 key 去重（避免重复运行写重复行）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seen = set()
    if key and os.path.exists(path):
        try:
            for line in open(path):
                if line.strip():
                    seen.add(json.loads(line).get(key))
        except Exception:
            seen = set()
    added = 0
    with open(path, "a") as f:
        for r in rows:
            if key and r.get(key) in seen:
                continue
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            added += 1
    return added


def detect_alerts(prev, cur, new_bases):
    """异动：价格(24h 涨跌) / OI 变化(对比基线) / 费率异常 / 新上市。返回事件列表。"""
    out = []
    for base, row in cur.items():
        pc = row.get("pc")
        if pc is not None and abs(pc) >= 15:
            out.append({"sym": base, "kind": "price", "val": round(pc, 2),
                        "msg": f"24h {pc:+.1f}%"})
        fr = row.get("fr")
        if fr is not None and abs(fr) >= 0.001 and row.get("_hl") is not True:
            out.append({"sym": base, "kind": "funding", "val": round(fr * 100, 4),
                        "msg": f"费率 {fr*100:+.3f}%"})
        old = (prev or {}).get(base)
        oi, oi0 = row.get("oi"), (old or {}).get("oi")
        if oi and oi0:
            chg = (oi - oi0) / oi0 * 100
            if abs(chg) >= 10:
                out.append({"sym": base, "kind": "oi", "val": round(chg, 1),
                            "msg": f"OI {chg:+.1f}%（{oi0/1e6:.1f}M→{oi/1e6:.1f}M）"})
    for base in new_bases:
        out.append({"sym": base, "kind": "new_listing", "val": None, "msg": "首次出现在扫描里"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--days", type=int, default=30, help="币安日级 OI 窗口（接口硬上限 ~30 天）")
    ap.add_argument("--long-days", type=int, default=180,
                    help="长窗口 gap 的天数（Gate 日级 OI，可达 1000 天）；0=关闭")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=12)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个币（调试）")
    ap.add_argument("--only", default="", help="只跑指定币（逗号分隔，调试）")
    ap.add_argument("--mode", choices=("fast", "half", "hour", "h4", "full"), default="hour",
                    help="fast=8 分钟行情+异动；half=30 分钟（行情+基金时序）；"
                         "hour=1 小时全量 gap；h4=4 小时（强制写每币文件+归档）；full=同 h4 但不归档")
    ap.add_argument("--no-gap", action="store_true")
    ap.add_argument("--union", action="store_true",
                    help="币种清单用全平台并集（默认只用币安永续）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    out = a.out
    meta_dir = os.path.join(out, "_meta")
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(os.path.join(out, "groups"), exist_ok=True)
    c = Client(proxy=a.proxy or None, timeout=a.timeout)
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    month = time.strftime("%Y%m", time.gmtime(now))

    # ---- A. 各平台行情（全量请求）----
    venue_rows, venue_meta = {}, {}
    for name, fn in VENUES:
        t = time.time()
        try:
            rows = fn(c)
            if name == "hyperliquid":
                for r in rows.values():
                    r["_hl"] = True      # HL 是 1h 费率，不与 8h 口径混
            venue_rows[name] = rows
            venue_meta[name] = {"ok": True, "n": len(rows), "s": round(time.time() - t, 1)}
        except Exception as e:
            venue_rows[name] = {}
            venue_meta[name] = {"ok": False, "n": 0, "s": round(time.time() - t, 1), "err": str(e)[:160]}

    # 代币清单 = 币安 USDT 永续（用户 2026-11-22 定：追踪范围为币安的币在各平台/链上的表现，
    # 用 --union 才扩展到全平台并集——并集会到 1500+ 币，仓库和数据量都吃不住）。
    bn_bases = set(venue_rows.get("binance", {}))
    all_bases = sorted(bn_bases if not a.union else set().union(*[set(r) for r in venue_rows.values()]))
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    if only:
        all_bases = [b for b in all_bases if b in only]
    if a.limit:
        all_bases = all_bases[: a.limit]

    # ---- B. 币安保险基金分组 ----
    groups, gate_fund = [], None
    try:
        groups = binance_groups(c)
    except Exception as e:
        venue_meta.setdefault("binance", {})["groups_err"] = str(e)[:120]
    try:
        gate_fund = gate_insurance(c)
    except Exception:
        gate_fund = None
    gmap = {g["gid"]: g for g in groups}
    sym2group = {}
    for g in groups:
        for s in g["symbols"]:
            sym2group[str(s).upper()] = g["gid"]

    bn = venue_rows.get("binance", {})
    base_gap_targets = [b for b in all_bases if b in bn]

    def base_gid(base):
        return sym2group.get(str((bn.get(base) or {}).get("sym") or (base + "USDT")).upper())

    def base_fund(base):
        gid = base_gid(base)
        g = gmap.get(gid) if gid else None
        return g["fund_usd"] if g else None

    # ---- fast 档：只抓行情 + 异动，不跑 gap、不写每币文件 ----
    prev_live = _read_json(os.path.join(meta_dir, "live.json"), {}) or {}
    prev_syms = prev_live.get("symbols") or {}
    cur_compact = {}
    for b in all_bases:
        row = bn.get(b) or {}
        venues_n = sum(1 for v in venue_rows.values() if b in v)
        best_oi = 0.0
        fr = row.get("fr")
        fr_src = "binance" if fr is not None else None
        if fr is None:
            for name, v in venue_rows.items():
                x = v.get(b)
                if x and x.get("oi"):
                    best_oi = max(best_oi, x["oi"])
                if fr is None and x and x.get("fr") is not None:
                    fr, fr_src = x["fr"], name
        if row.get("oi"):
            best_oi = max(best_oi, row["oi"])
        else:
            for v in venue_rows.values():
                x = v.get(b)
                if x and x.get("oi"):
                    best_oi = max(best_oi, x["oi"])
        cur_compact[b] = {"p": row.get("p") or next((v[b]["p"] for v in venue_rows.values() if b in v and v[b].get("p")), None),
                          "pc": row.get("pc"), "oi": best_oi or None, "fr": fr,
                          "v": row.get("v") or max([v[b].get("v") or 0 for v in venue_rows.values() if b in v] or [0]) or None,
                          "nv": venues_n, "_hl": fr_src == "hyperliquid"}
    new_bases = [b for b in cur_compact if b not in prev_syms] if prev_syms else []
    alerts = detect_alerts(prev_syms, cur_compact, new_bases)
    alert_rows = [{"ts": now, "sym": x["sym"], "kind": x["kind"], "val": x["val"],
                   "msg": x["msg"]} for x in alerts]

    # 基金组变化：对比上次组基金
    prev_fund = _read_json(os.path.join(meta_dir, "fund.json"), {}) or {}
    prev_g = {x["gid"]: x.get("fund_usd") for x in (prev_fund.get("groups") or [])}
    fund_alerts = []
    for g in groups:
        old = prev_g.get(g["gid"])
        if old and g["fund_usd"] and abs(g["fund_usd"] - old) / old >= 0.05:
            fund_alerts.append({"ts": now, "sym": g["gid"], "kind": "fund",
                                "val": round((g["fund_usd"] - old) / old * 100, 2),
                                "msg": f"组 {g['gid']} 基金 {old/1e6:.2f}M→{g['fund_usd']/1e6:.2f}M"})
    if fund_alerts:
        alert_rows += fund_alerts

    # 基线节流：每 2 小时写一次（异动判断用；写太勤会让仓库膨胀）
    live_age = now - int(prev_live.get("updated") or 0)
    write_live = (a.mode == "fast" and (not prev_live or live_age >= 2 * 3600)) or a.mode != "fast"
    if write_live:
        _write_json(os.path.join(meta_dir, "live.json"),
                    {"updated": now, "symbols": cur_compact})
        _write_json(os.path.join(meta_dir, "fund.json"),
                    {"updated": now, "gate_usdt": gate_fund,
                     "groups": [{"gid": g["gid"], "fund_usd": g["fund_usd"]} for g in groups]})

    squeeze_docs = {}
    if a.mode in ("fast", "half"):
        # 短时爆拉检测：8m 3% / 30m 6% / 1h 10% / 4h 20%（按成交额取前 150 个币，控请求量）
        ranked = sorted(cur_compact.items(), key=lambda kv: -(kv[1].get("v") or 0))[:150]

        def tick_job(base):
            pair = (bn.get(base) or {}).get("sym") or (base + "USDT")
            try:
                return base, window_returns(c, pair)
            except Exception:
                return base, None

        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            futs = [ex.submit(tick_job, b) for b, _ in ranked]
            for fut in as_completed(futs):
                base, rets = fut.result()
                hits = _squeeze_hit(rets)
                if not hits:
                    continue
                f = base_fund(base)
                try:
                    rows = daily_rows(c, (bn.get(base) or {}).get("sym") or (base + "USDT"), a.days)
                    ladder = short_ladder(rows, 0.5)
                    cur = (bn.get(base) or {}).get("p") or (rows[-1][5] if rows else None)
                    sq = squeeze_windows(ladder, cur, rets, f)
                except Exception:
                    sq = []
                if not sq:
                    continue
                squeeze_docs[base] = {"symbol": base, "updated": now, "price": cur,
                                      "windows": sq,
                                      "group": {"gid": base_gid(base), "fund_usd": f},
                                      "method": "short_ladder_30d_lifo"}
                for x in sq:
                    alert_rows.append({
                        "ts": now, "sym": base, "kind": f"squeeze_{x['w']}",
                        "val": x["ret_pct"],
                        "msg": (f"{x['w']} 爆拉 {x['ret_pct']:+.1f}% → 扫空头 ${x['N']/1e6:.2f}M，"
                                f"损失 ${x['L']/1e3:.0f}K"
                                + (f"（组基金 {x['uL']*100:.1f}%）" if x.get("uL") else "")),
                    })

    if alert_rows:
        apath = os.path.join(out, "_meta", "alerts", f"{today}.jsonl")
        seen_alert = set()
        if os.path.exists(apath):
            try:
                for line in open(apath):
                    if line.strip():
                        x = json.loads(line)
                        seen_alert.add((x.get("sym"), x.get("kind")))
            except Exception:
                seen_alert = set()
        fresh = [x for x in alert_rows if (x["sym"], x["kind"]) not in seen_alert]
        if fresh:
            _append_jsonl(apath, fresh, key=None)

    if a.mode == "fast":
        scan = {"updated": now, "mode": "fast", "elapsed_s": round(time.time() - t0, 1),
                "bases": len(all_bases), "alerts": len(alert_rows), "venues": venue_meta,
                "live_written": write_live}
        _write_json(os.path.join(meta_dir, "scan.json"), scan)
        if a.json:
            print(json.dumps(scan, ensure_ascii=False, indent=1))
        else:
            print(f"[{time.strftime('%m-%d %H:%M')}] fast：{len(all_bases)} 币 · "
                  f"异动 {len(alert_rows)} 条 · 基线{'已' if write_live else '未'}更新 · "
                  f"{scan['elapsed_s']}s")
        return 0

    if a.mode == "half":
        # 30 分钟档：只写全市场一行 + 基金时序（每币文件留给 hour/h4）
        market_index = {}
        for base in all_bases:
            row = cur_compact.get(base) or {}
            venues_n = row.get("nv") or 0
            market_index[base] = {"p": row.get("p"), "v": row.get("v"), "oi": row.get("oi"),
                                  "fr": row.get("fr"), "gap": None, "max_gap": None,
                                  "nv": venues_n, "gid": sym2group.get((bn.get(base) or {}).get("sym", base + "USDT").upper()) if bn else None}
        _write_json(os.path.join(meta_dir, "market.json"),
                    {"updated": now, "mode": "half", "symbols": market_index})
        _maybe_fund_series(out, groups, now, month, meta_dir, force=False)
        scan = {"updated": now, "mode": "half", "elapsed_s": round(time.time() - t0, 1),
                "bases": len(all_bases), "alerts": len(alert_rows), "venues": venue_meta,
                "groups": len(groups), "gate_insurance_usdt": gate_fund}
        _write_json(os.path.join(meta_dir, "scan.json"), scan)
        if a.json:
            print(json.dumps(scan, ensure_ascii=False, indent=1))
        else:
            print(f"[{time.strftime('%m-%d %H:%M')}] half：{len(all_bases)} 币 · "
                  f"异动 {len(alert_rows)} · {scan['elapsed_s']}s")
        return 0

    # ---- hour/full：全量 gap（币安有合约的币）----
    gaps = {}
    if base_gap_targets and not a.no_gap:
        def job(base):
            pair = bn[base]["sym"]
            try:
                rows = daily_rows(c, pair, a.days)
                if len(rows) < 3:
                    return base, {"ok": False, "err": "日级数据不足"}
                hist = gap_series(rows, 0.5, a.days)
                cur = compute_gap(rows[-a.days:], rows[-1][5], 0.5)
                best = max((x for x in hist if x.get("gap") is not None),
                           key=lambda x: x["gap"], default=None)
                long_gap = None
                if a.long_days and a.mode in ("full", "h4"):
                    try:
                        long_gap = long_window_gap(c, base, base + "_USDT", a.long_days,
                                                   bn[base].get("p"))
                    except Exception:
                        long_gap = None
                rets, sq = None, []
                try:
                    rets = window_returns(c, pair)
                    if rets:
                        cur_px = bn[base].get("p") or rows[-1][5]
                        sq = squeeze_windows(short_ladder(rows, 0.5), cur_px, rets,
                                             base_fund(base))
                except Exception:
                    rets, sq = None, []
                return base, {"ok": True, "gap": cur, "hist": hist, "best": best,
                              "days": len(rows), "rets": rets, "squeeze": sq,
                              "long_window": long_gap}
            except Exception as e:
                return base, {"ok": False, "err": str(e)[:120]}

        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            futs = [ex.submit(job, b) for b in base_gap_targets]
            for fut in as_completed(futs):
                try:
                    base, res = fut.result()
                    gaps[base] = res
                except Exception:
                    continue

    # ---- 写每币文件夹 ----
    market_index = {}
    ok_gap = 0
    for base in all_bases:
        folder = os.path.join(out, base)
        venues = {}
        for name, rows in venue_rows.items():
            row = rows.get(base)
            if row:
                venues[name] = {k: v for k, v in row.items() if v is not None and k != "_hl"}
        g = gaps.get(base) or {}
        gap, hist, best = g.get("gap"), g.get("hist") or [], g.get("best")
        bn_sym = (bn.get(base) or {}).get("sym") or (base + "USDT")
        gid = sym2group.get(bn_sym.upper())
        grp = gmap.get(gid) if gid else None
        group_max = None
        if grp:
            for m in grp["symbols"]:
                bst = (gaps.get(_base(m)) or {}).get("best")
                if bst and bst.get("gap") is not None:
                    if not group_max or bst["gap"] > group_max["gap"]:
                        group_max = {"symbol": _base(m), "gap": bst["gap"], "day": bst["d"]}
        gap_ratio = gap.get("gap_ratio") if gap else None
        gap_doc = {
            "symbol": base, "pair": bn_sym,
            "price": (bn.get(base) or {}).get("p"),
            "updated": now,
            "gap": gap if gap_ratio is not None else None,
            "max_gap_30d": best,
            "group": ({"gid": gid, "fund_usd": grp["fund_usd"],
                       "members": [_base(x) for x in grp["symbols"]],
                       "group_max_gap": group_max} if grp else None),
            "method": "lifo_30d_binance_daily",
            "assumptions": "LIFO 30天 · 杠杆 10/8/5/3/2 (30/15/30/15/10) · MMR 0.4% · long_share 0.5",
        }
        if g.get("long_window"):
            gap_doc["long_window"] = g["long_window"]
        if g.get("squeeze"):
            gap_doc["window_ret_pct"] = g.get("rets")
            gap_doc["squeeze"] = g["squeeze"]
            mx = max(g["squeeze"], key=lambda x: (x.get("uL") or 0))
            gap_doc["squeeze_max"] = mx
        if gap_ratio is not None:
            ok_gap += 1
            gpath = os.path.join(folder, "gap.json")
            old = _read_json(gpath, {}) or {}
            old_ratio = ((old.get("gap") or {}).get("gap_ratio"))
            same_day = str(old.get("day") or "") == today
            changed = (old_ratio is None or abs(gap_ratio - old_ratio) / max(abs(old_ratio), 1e-9) >= 0.05)
            if a.mode in ("full", "h4") or changed or not same_day:
                gap_doc["day"] = today
                os.makedirs(folder, exist_ok=True)
                _write_json(gpath, gap_doc)
                _append_jsonl(os.path.join(folder, "gap_daily.jsonl"),
                              [{"d": x["d"], "gap": x["gap"], "lp": x["lp"], "sa": x["sa"]}
                               for x in hist if x.get("d")], key="d")
                # 只保留最近 120 天
                try:
                    hp = os.path.join(folder, "gap_daily.jsonl")
                    lines = [x for x in open(hp).read().splitlines() if x.strip()]
                    if len(lines) > 120:
                        open(hp, "w").write("\n".join(lines[-120:]) + "\n")
                except Exception:
                    pass
        chain = None
        try:
            prof = _load_profiles().get(base) or {}
            if prof.get("network") or prof.get("jetton"):
                chain = {"network": prof.get("network"), "jetton": prof.get("jetton")}
        except Exception:
            chain = None
        if venues:
            vpath = os.path.join(folder, "venues.json")
            old_v = _read_json(vpath, {}) or {}
            static_now = {k: {kk: vv for kk, vv in v.items() if kk in ("sym", "lev", "onboard", "v")}
                          for k, v in venues.items()}
            old_static = {k: {kk: vv for kk, vv in v.items() if kk in ("sym", "lev", "onboard", "v")}
                          for k, v in (old_v.get("venues") or {}).items()}
            if a.mode in ("full", "h4") or static_now != old_static:
                os.makedirs(folder, exist_ok=True)
                _write_json(vpath, {"symbol": base, "updated": now, "venues": venues,
                                    **({"chain": chain} if chain else {})})
        market_index[base] = {
            "p": (bn.get(base) or {}).get("p") or cur_compact.get(base, {}).get("p"),
            "v": max([x.get("v") or 0 for x in venues.values()] or [0]),
            "oi": max([x.get("oi") or 0 for x in venues.values()] or [0]),
            "fr": (bn.get(base) or {}).get("fr") or cur_compact.get(base, {}).get("fr"),
            "gap": gap_ratio, "max_gap": best.get("gap") if best else None,
            "nv": len(venues), "gid": gid,
        }

    # ---- 短时爆拉（hour/h4 的 squeeze 结果）：写 squeeze.json + 告警行 ----
    for base, g in gaps.items():
        sq = g.get("squeeze") or []
        if not sq:
            continue
        f = base_fund(base)
        squeeze_docs[base] = {"symbol": base, "updated": now,
                              "price": (bn.get(base) or {}).get("p"),
                              "windows": sq,
                              "group": {"gid": base_gid(base), "fund_usd": f},
                              "method": "short_ladder_30d_lifo"}
        hits = _squeeze_hit(g.get("rets") or {})
        if hits:
            for w, z in hits:
                x = next((y for y in sq if y["w"] == w), None)
                if not x:
                    continue
                alert_rows.append({
                    "ts": now, "sym": base, "kind": f"squeeze_{w}", "val": z,
                    "msg": (f"{w} 爆拉 {z:+.1f}% → 扫空头 ${x['N']/1e6:.2f}M，"
                            f"损失 ${x['L']/1e3:.0f}K"
                            + (f"（组基金 {x['uL']*100:.1f}%）" if x.get("uL") else "")),
                })
    for base, doc in squeeze_docs.items():
        os.makedirs(os.path.join(out, base), exist_ok=True)
        _write_json(os.path.join(out, base, "squeeze.json"), doc)
    if alert_rows:
        apath = os.path.join(out, "_meta", "alerts", f"{today}.jsonl")
        seen_alert = set()
        if os.path.exists(apath):
            try:
                for line in open(apath):
                    if line.strip():
                        x = json.loads(line)
                        seen_alert.add((x.get("sym"), x.get("kind")))
            except Exception:
                seen_alert = set()
        fresh = [x for x in alert_rows if (x["sym"], x["kind"]) not in seen_alert]
        if fresh:
            _append_jsonl(apath, fresh, key=None)

    # ---- 组文件 ----
    group_index = {}
    for g in groups:
        members, best_all, cur_all = [], None, None
        for s in g["symbols"]:
            b = _base(s)
            if b not in members:
                members.append(b)
            res = gaps.get(b) or {}
            bst = res.get("best")
            if bst and bst.get("gap") is not None:
                if not best_all or bst["gap"] > best_all["gap"]:
                    best_all = {"symbol": b, "gap": bst["gap"], "day": bst["d"]}
            cg = (res.get("gap") or {}).get("gap_ratio")
            if cg is not None and (cur_all is None or cg > cur_all["gap"]):
                cur_all = {"symbol": b, "gap": cg}
        sq_max = None
        for b in members:
            sq = (gaps.get(b) or {}).get("squeeze") or []
            for x in sq:
                if x.get("uL") is not None and (not sq_max or x["uL"] > sq_max.get("uL", 0)):
                    sq_max = {"symbol": b, "w": x["w"], "ret_pct": x["ret_pct"],
                              "uL": x["uL"], "uN": x.get("uN"), "L": x["L"]}
        long_max = None
        for b in members:
            lw = (gaps.get(b) or {}).get("long_window") or {}
            m = lw.get("max_gap")
            if m and (long_max is None or m["gap"] > long_max["gap"]):
                long_max = {"symbol": b, "gap": m["gap"], "day": m["d"],
                            "window_days": lw.get("window_days")}
        doc = {"gid": g["gid"], "fund_usd": g["fund_usd"], "updated": now,
               "symbols": g["symbols"], "members": members,
               "group_max_gap_30d": best_all, "group_current_max_gap": cur_all,
               "group_squeeze_max": sq_max, "group_long_max_gap": long_max}
        _write_json(os.path.join(out, "groups", f"{g['gid']}.json"), doc)
        group_index[g["gid"]] = {"fund_usd": g["fund_usd"], "n": len(g["symbols"]),
                                 "max_gap": best_all, "cur_max": cur_all}

    # ---- 基金时序（每小时一行/组，供图 16 ① 层；带节流）----
    _maybe_fund_series(out, groups, now, month, meta_dir,
                       force=(a.mode in ("full", "h4")))

    # ---- 全局元数据 ----
    scan = {"updated": now, "mode": a.mode, "elapsed_s": round(time.time() - t0, 1),
            "bases": len(all_bases), "gap_ok": ok_gap, "gap_targets": len(base_gap_targets),
            "groups": len(groups), "gate_insurance_usdt": gate_fund,
            "alerts": len(alert_rows), "venues": venue_meta, "days": a.days}
    _write_json(os.path.join(meta_dir, "scan.json"), scan)
    _write_json(os.path.join(meta_dir, "market.json"), {"updated": now, "symbols": market_index})
    _write_json(os.path.join(out, "groups", "index.json"), {"updated": now, "groups": group_index})

    if a.json:
        print(json.dumps(scan, ensure_ascii=False, indent=1))
    else:
        print(f"[{time.strftime('%m-%d %H:%M')}] {a.mode}：{len(all_bases)} 币 · "
              f"gap {ok_gap}/{len(base_gap_targets)} · {len(groups)} 组 · "
              f"异动 {len(alert_rows)} · {scan['elapsed_s']}s")
        for name, m in venue_meta.items():
            flag = "OK" if m.get("ok") else "缺失"
            print(f"  {name:12s} {flag:4s} {m.get('n', 0):4d} 个 {m.get('s', 0)}s"
                  + (f"  {m.get('err', '')}" if not m.get("ok") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
