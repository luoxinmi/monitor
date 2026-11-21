#!/usr/bin/env python3
"""兑现缺口（gap_ratio）：GitHub 端产出、本机只消费。

为什么要放云端跑：
  - 本机只在分析时才有数据；云端每 5 分钟/每小时采样，能攒出缺口的时序变化；
  - 数据落仓库（workflow 的 commit step），不受 Actions 日志 90 天保留期限制。

口径（与本地 estimate.py 的 LIFO 成本分层同源，但数据源换成币安 fapi）：
  - 日级 OI 名义（sumOpenInterestValue）+ 日线 OHLC（klines 1d，最近 30 天）
  - LIFO：底仓 = 窗口起点 OI；之后正增量入层（成本 = 当日 (O+H+L+C)/4），
    负增量从最新层往底仓扣
  - 多头浮盈 long_pnl  = Σ 层名义 × (1 − 成本/现价)
  - 存活空单 short_alive = Σ 层名义 × (1−多空份额) × 杠杆权重，只算「强平价 > 现价」的
  - gap_ratio = long_pnl / short_alive
    含义：多头一起兑现时，还活着的空头对手盘够不够接（越大越危险）。

假设（写进输出，别当官方数据）：
  杠杆档 10/8/5/3/2 权重 30/15/30/15/10（经验分布，与本地 auto_leverage 同档）；
  MMR 0.4%（云端不查分档表，用币安首档附近的经验值）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

# 兑现缺口的判读阈值（2026-11-22 用户定：防妖币极端逼空，要找「最大缺口」）：
#   gap_ratio < GAP_MIN_RATIO 时，视为「没有缺口/对手盘充足」，用 GAP_PRESET_RATIO 代替显示，
#   避免小数字被误读成信号，也让图 16 / 告警始终有可比的基准值。
# 两个数都可用环境变量覆盖（云端 workflow 里设 env 即可；默认 0 = 不启用预设值）。
GAP_MIN_RATIO = float(os.environ.get("GAP_MIN_RATIO", "0"))
GAP_PRESET_RATIO = float(os.environ.get("GAP_PRESET_RATIO", "1.0"))

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
BN_HOSTS = ("https://fapi.binance.com", "https://www.binance.com")

LEVS = ((10, 0.30), (8, 0.15), (5, 0.30), (3, 0.15), (2, 0.10))
MM = 0.004


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


def bn_json(path, proxy=None, timeout=25):
    """币安 fapi：直连失败自动换 www 透传域名（云端 runner 在美国，fapi 会 451）。"""
    last = None
    for host in BN_HOSTS:
        try:
            return get_json(host + path, proxy, timeout)
        except Exception as e:
            last = e
    raise last


def fetch_daily(pair, proxy=None, days=30):
    """币安日级 OI（名义）+ 日线 OHLC 对齐 → [(day, oi_usd, o, h, l, c), ...]。

    注意：币安 openInterestHist 只保留最近约 30 天，1d 间隔最多 30 条——这是云端 30 天窗口的硬上限。
    """
    oi = bn_json(f"/futures/data/openInterestHist?symbol={pair}&period=1d&limit={days}", proxy)
    kl = bn_json(f"/fapi/v1/klines?symbol={pair}&interval=1d&limit={days}", proxy)
    if not isinstance(oi, list) or not isinstance(kl, list):
        return []
    klmap = {}
    for k in kl:
        try:
            day = int(k[0]) // 86400000
            klmap[day] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    rows = []
    for x in oi:
        try:
            day = int(x["timestamp"]) // 86400000
            if day not in klmap:
                continue
            o, h, l, c = klmap[day]
            rows.append((str(day), float(x["sumOpenInterestValue"]), o, h, l, c))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort()
    return rows


def fetch_long_share(pair, proxy=None):
    """币安大户持仓量多空比（>1 表示大户净多）；取不到就退回 0.5。"""
    try:
        d = bn_json(f"/futures/data/topLongShortPositionRatio?symbol={pair}&period=1d&limit=1", proxy)
        if d:
            return float(d[-1]["longAccount"])
    except Exception:
        pass
    return 0.5


def layers_once(daily, cur, base_days=30):
    """LIFO 成本分层（与 local estimate.py 的同名算法一致）。"""
    base_idx = max(0, len(daily) - (base_days + 1))
    base = daily[base_idx]
    layers = [[base[0], base[5], base[1]]]        # [day, cost, notional]
    for i in range(base_idx + 1, len(daily)):
        delta = daily[i][1] - daily[i - 1][1]
        if delta > 0:
            cost = (daily[i][3] + daily[i][4] + daily[i][5]) / 3
            layers.append([daily[i][0], cost, delta])
        elif delta < 0:
            cut = -delta
            while cut > 1e-9 and layers:          # LIFO：新层扣完再扣底仓
                last = layers[-1]
                take = min(last[2], cut)
                last[2] -= take
                cut -= take
                if last[2] <= 1e-9:
                    layers.pop()
    return [x for x in layers if x[2] > 1e-9]


def compute_gap(daily, cur, long_share=0.5, mm=MM, levs=LEVS):
    """返回兑现缺口字典；数据不足返回 None。"""
    if not daily or not cur or cur <= 0:
        return None
    layers = layers_once(daily, cur)
    if not layers:
        return None
    total = sum(x[2] for x in layers)
    long_pnl = sum(amt * (1 - cost / cur) for _, cost, amt in layers)
    short_alive = 0.0
    for _, cost, amt in layers:
        for lev, w in levs:
            lp = cost * (1 + 1 / lev - mm)        # 空单强平价
            if lp > cur:                          # 只算还活着的空单
                short_alive += amt * (1 - long_share) * w
    raw_gap = (long_pnl / short_alive) if short_alive > 1e-9 else None
    # 低于阈值时用预设值代替（capped 字段说明是哪条规则生效；raw 保留原值便于回查）
    gap, capped = raw_gap, None
    if raw_gap is not None and GAP_MIN_RATIO > 0 and raw_gap < GAP_MIN_RATIO:
        gap = GAP_PRESET_RATIO
        capped = "below_min"
    return {
        "gap_ratio": round(gap, 3) if gap is not None else None,
        "gap_ratio_raw": round(raw_gap, 3) if raw_gap is not None else None,
        "capped": capped,
        "min_ratio": GAP_MIN_RATIO or None,
        "preset_ratio": GAP_PRESET_RATIO if GAP_MIN_RATIO > 0 else None,
        "long_pnl_usd": round(long_pnl, 2),
        "short_alive_usd": round(short_alive, 2),
        "total_oi_usd": round(total, 2),
        "layers": len(layers),
        "long_share": round(float(long_share), 4),
        "window_days": 30,
        "method": "lifo30_binance",
        "assumptions": "LIFO 30天 · 杠杆 10/8/5/3/2 (30/15/30/15/10) · MMR 0.4%",
        "ts": int(time.time()),
    }


def main():
    ap = argparse.ArgumentParser(description="兑现缺口采样（币安 fapi 30 天）")
    ap.add_argument("--pair", default="EVAAUSDT", help="币安合约名，如 EVAAUSDT")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    daily = fetch_daily(a.pair, a.proxy, a.days)
    if not daily:
        print("没有日级 OI/K 线数据（该币没有币安永续或接口不可达）", file=sys.stderr)
        return 1
    cur = daily[-1][5]
    ls = fetch_long_share(a.pair, a.proxy)
    gap = compute_gap(daily, cur, ls)
    out = {"pair": a.pair, "price": cur, "gap": gap}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"{a.pair} 现价 {cur:g} · {len(daily)} 天日级数据")
        if gap:
            print(f"  兑现缺口 {gap['gap_ratio']}× = 多头浮盈 ${gap['long_pnl_usd']/1e6:.2f}M"
                  f" / 存活空单 ${gap['short_alive_usd']/1e6:.2f}M · {gap['layers']} 层")
            print(f"  假设：{gap['assumptions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
