#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""白鲸节点 · GitHub Actions 侧连通性探针

由 .github/workflows/whale-probe.yml 调用，只读探测，不改动仓库其它内容。
输出 probe-result.json + 控制台 markdown 表。
"""
import json, os, subprocess, sys, time, socket
from pathlib import Path

UUID = "be58f4e4-894a-47ee-8836-cf75e74fa9c9"
RELAY_PORT = int(os.getenv("RELAY_PORT", "19233"))
ROUNDS = int(os.getenv("ROUNDS", "5"))
TIMEOUT = int(os.getenv("TIMEOUT", "20"))
OUT = Path("probe-result.json")

ZONES = {
    "AUTO":  "157.255.68.184:9233",
    "HK-01": "157.255.68.184:9371",
    "HK-02": "183.232.74.235:9233",
    "TW-02": "157.255.68.184:9382",
    "US-03": "157.255.68.184:9388",
    "JP-04": "157.255.68.184:9377",
    "SG-02": "157.255.68.184:9380",
    "UK-01": "157.255.68.184:9385",
}
if os.getenv("ZONES"):
    want = [z.strip() for z in os.environ["ZONES"].split(",") if z.strip()]
    ZONES = {k: v for k, v in ZONES.items() if k in want}


def free_port(start=19300):
    for p in range(start, start + 500):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p)); return p
            except OSError:
                continue
    raise RuntimeError("no free port")


def curl(proxy=None, url="http://ipv4.icanhazip.com"):
    cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(TIMEOUT), "-w", "%{http_code} %{time_total}"]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
        parts = r.stdout.split()
        if len(parts) == 2:
            return parts[0], round(float(parts[1]), 2)
    except Exception:
        pass
    return "ERR", round(time.time() - t0, 2)


def exit_ip(proxy):
    try:
        r = subprocess.run(["curl", "-s", "-m", str(TIMEOUT), "-x", proxy, "http://ipv4.icanhazip.com"],
                           capture_output=True, text=True, timeout=TIMEOUT + 5)
        v = (r.stdout or "").strip()
        return v if v and "<" not in v else None
    except Exception:
        return None


def test_zone(tag, target, xray):
    socks = free_port()
    cfg = {
        "log": {"loglevel": "error"},
        "inbounds": [{"port": socks, "listen": "127.0.0.1", "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": True}}],
        "outbounds": [{
            "protocol": "vmess",
            "settings": {"vnext": [{"address": "127.0.0.1", "port": RELAY_PORT,
                                    "users": [{"id": UUID, "alterId": 0, "level": 8,
                                               "security": "auto"}]}]},
            "streamSettings": {"network": "ws", "security": "",
                               "wsSettings": {"path": "/",
                                              "headers": {"Host": target}}}
        }]
    }
    p = Path(f"/tmp/xr_{tag}.json")
    p.write_text(json.dumps(cfg, ensure_ascii=False))
    proc = subprocess.Popen([xray, "run", "-c", str(p)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    codes = []
    lat = []
    try:
        for _ in range(ROUNDS):
            c, t = curl(f"socks5h://127.0.0.1:{socks}")
            codes.append(c)
            if c == "200":
                lat.append(t)
            time.sleep(0.6)
        eip = exit_ip(f"socks5h://127.0.0.1:{socks}")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    ok = sum(1 for c in codes if c == "200")
    return {"zone": tag, "target": target, "ok": ok, "total": ROUNDS,
            "rate": round(ok / ROUNDS * 100), "codes": codes,
            "latency_avg": round(sum(lat) / len(lat), 2) if lat else None,
            "exit_ip": eip}


def main():
    xray = os.getenv("XRAY_BIN", "./xray")
    if not Path(xray).exists():
        print(f"✗ 找不到 {xray}"); sys.exit(1)

    print("## 白鲸 · Actions 侧连通性\n")
    # 基线：直连
    base_codes, base_lat = [], []
    for _ in range(3):
        c, t = curl(None)
        base_codes.append(c)
        if c == "200": base_lat.append(t)
        time.sleep(0.5)
    base_ok = sum(1 for c in base_codes if c == "200")
    print(f"**基线（runner 直连）**：{base_ok}/3 成功\n")

    results = []
    for tag, target in ZONES.items():
        print(f"测试 {tag} ({target}) …", flush=True)
        r = test_zone(tag, target, xray)
        results.append(r)
        print(f"  → {r['ok']}/{r['total']} ({r['rate']}%)  出口 {r['exit_ip'] or '无'}  "
              f"延迟 {r['latency_avg'] or '-'}s\n", flush=True)

    print("\n| 区域 | 接入点 | 成功 | 成功率 | 出口 IP | 平均延迟 |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['zone']} | `{r['target']}` | {r['ok']}/{r['total']} | "
              f"{r['rate']}% | {r['exit_ip'] or '-'} | {r['latency_avg'] or '-'}s |")

    OUT.write_text(json.dumps({
        "runner": os.getenv("RUNNER_NAME", "unknown"),
        "at": int(time.time()),
        "baseline": {"ok": base_ok, "total": 3, "codes": base_codes},
        "zones": results,
    }, ensure_ascii=False, indent=2))
    print(f"\n结果已写 {OUT}")


if __name__ == "__main__":
    main()
