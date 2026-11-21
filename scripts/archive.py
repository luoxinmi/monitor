#!/usr/bin/env python3
"""仓库膨胀自动归档：data/ 超过阈值 → 打包发 Release → 清空历史，Release 说明写关键数据。

为什么这么做（GitHub 存储策略）：
  - Public 仓库的 **Releases 不限量**（单个 asset ≤ 2GB），而仓库 git 历史会一直膨胀；
  - 定期把 data/ 打包成 release asset（tag `data-YYYYMMDD-HHMM`），再把仓库历史 squash 成
    一个提交，只留最新状态 —— 仓库占用回到起点，历史数据随时能从 Release 下载。

Release 说明（body）写的是**关键数据**，即使数据被清空也能直接看：
  - 扫描概况（币数 / gap 覆盖 / 基金组数 / 基金总额）
  - 每个代币在**自己组里**造成的最大基金缺口 Top N（图 16 防妖币要用的那个数）
  - 组基金池 Top N
  - 告警统计

用法（workflow 里，需要 contents: write）：
  GH_TOKEN=... GH_REPO=owner/repo python3 scripts/archive.py \
      --data data --threshold-mb 1024 --keep-days 30 [--dry-run] [--squash]

没有 GH_TOKEN 时只做「清理旧明细 + 打印摘要」，不发 Release（本地调试用）。
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "dsh-archive", "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------- 工具
def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def gh(method, url, token, payload=None, raw=None, content_type=None):
    data = None
    headers = dict(UA)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        return r.status, (json.loads(body.decode()) if body else {})


# ---------------------------------------------------------------- 摘要（Release 说明）
def build_summary(data_dir, stats):
    """从 groups/ 与 _meta/ 汇总关键数据：组内最大缺口 / 组基金池。"""
    idx = read_json(os.path.join(data_dir, "groups", "index.json"), {}) or {}
    scan = read_json(os.path.join(data_dir, "_meta", "scan.json"), {}) or {}
    fund_meta = read_json(os.path.join(data_dir, "_meta", "fund.json"), {}) or {}
    gs = idx.get("groups") or {}
    # groups/<gid>.json 里有长窗口（180 天 Gate 口径）最大缺口；index.json 只有 30 天
    gdocs = []
    gdir = os.path.join(data_dir, "groups")
    if os.path.isdir(gdir):
        for f in sorted(os.listdir(gdir)):
            if f.startswith("g") and f.endswith(".json"):
                d = read_json(os.path.join(gdir, f), {}) or {}
                if d.get("gid"):
                    gdocs.append(d)
    long_by_gid = {d["gid"]: d.get("group_long_max_gap") for d in gdocs
                   if d.get("group_long_max_gap")}

    # 每个代币在组内造成的最大缺口：group_max_gap_30d 属于「组」，币是其中最大的那个
    per_coin = []
    for gid, g in gs.items():
        mg = g.get("max_gap") or {}
        if mg.get("gap") is None:
            continue
        lw = long_by_gid.get(gid) or {}
        per_coin.append({
            "symbol": mg.get("symbol"), "gid": gid, "gap": mg.get("gap"),
            "day": mg.get("day"), "fund_usd": g.get("fund_usd"),
            "long_gap": lw.get("gap"), "long_day": lw.get("day"),
            "long_symbol": lw.get("symbol"),
        })
    per_coin.sort(key=lambda x: -(x["gap"] or 0))

    funds = sorted([(gid, (g.get("fund_usd") or 0)) for gid, g in gs.items()],
                   key=lambda x: -x[1])[:10]
    total_fund = sum((g.get("fund_usd") or 0) for g in gs.values())
    gate_fund = scan.get("gate_insurance_usdt") or fund_meta.get("gate_usdt")

    lines = []
    lines.append(f"## 数据归档 {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    lines.append("")
    lines.append(f"- 代币数：**{scan.get('bases') or '?'}**（币安 USDT 永续）· "
                 f"gap 覆盖 **{scan.get('gap_ok') or len(gs) or '?'}** · 基金组 **{scan.get('groups') or len(gs) or '?'}**")
    lines.append(f"- 币安基金组合计 **${total_fund/1e6:.2f}M** · "
                 f"Gate 全站保险基金 **${(gate_fund or 0)/1e6:.2f}M**")
    if stats.get("data_mb") is not None:
        lines.append(f"- 归档数据：data/ {stats['data_mb']:.0f} MB · "
                     f".git {stats.get('git_mb', 0):.0f} MB · 保留明细最近 {stats.get('keep_days')} 天")
    lines.append("")
    lines.append("### 各代币在所在组造成的最大基金缺口（30 天）")
    lines.append("")
    lines.append("| # | 币 | 组 | 30 天最大缺口 | 日期 | 180 天最大缺口 | 日期 | 组基金 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, x in enumerate(per_coin[:20], 1):
        lg = f"| {x['long_gap']:.2f}×（{x['long_symbol']}）" if x.get("long_gap") else "| -"
        ld = f"| {x['long_day']}" if x.get("long_day") else "| -"
        lines.append(f"| {i} | {x['symbol']} | {x['gid']} | {x['gap']:.2f}× | {x['day'] or '-'} "
                     f"{lg} {ld} | ${(x['fund_usd'] or 0)/1e6:.2f}M |")
    if not per_coin:
        lines.append("| - | 暂无 | - | - | - | - |")
    lines.append("")
    lines.append("### 组基金池 Top 10")
    lines.append("")
    for gid, f in funds:
        lines.append(f"- {gid}: ${f/1e6:.2f}M")
    lines.append("")
    lines.append("> gap = 多头浮盈 / 存活空单（LIFO 30 天 · 杠杆 10/8/5/3/2 · MMR 0.4%）。"
                 "组内共用同一个保险基金池，所以按组看风险。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 打包
def make_tar(data_dir, out_path):
    with tarfile.open(out_path, "w:gz") as tf:
        for name in sorted(os.listdir(data_dir)):
            tf.add(os.path.join(data_dir, name), arcname=name)
    return out_path


# ---------------------------------------------------------------- 清理旧明细
def prune_old(data_dir, keep_days):
    """删掉太旧的明细（保留最新状态文件），返回 (删文件数, 删行数)。"""
    cutoff = time.time() - keep_days * 86400
    cutoff_day = time.strftime("%Y%m%d", time.gmtime(cutoff))
    cutoff_month = time.strftime("%Y%m", time.gmtime(cutoff))
    removed = 0
    # 1) alerts：按天文件
    adir = os.path.join(data_dir, "_meta", "alerts")
    if os.path.isdir(adir):
        for f in os.listdir(adir):
            if f.endswith(".jsonl") and f[:8].isdigit() and f[:8] < cutoff_day:
                os.remove(os.path.join(adir, f))
                removed += 1
    # 2) fund 时序：按月文件
    fdir = os.path.join(data_dir, "fund")
    if os.path.isdir(fdir):
        for f in os.listdir(fdir):
            if f.startswith("groups_") and f.endswith(".jsonl"):
                m = f[len("groups_"):-len(".jsonl")]
                if m < cutoff_month:
                    os.remove(os.path.join(fdir, f))
                    removed += 1
    # 3) 每币 gap_daily / history：只留 keep_days
    for base in os.listdir(data_dir):
        folder = os.path.join(data_dir, base)
        if not os.path.isdir(folder) or base.startswith("_"):
            continue
        limits = {"gap_daily.jsonl": 120, "history.jsonl": 2000}
        for fn, lim in limits.items():
            p = os.path.join(folder, fn)
            if not os.path.exists(p):
                continue
            try:
                lines = [x for x in open(p).read().splitlines() if x.strip()]
                keep = lines[-lim:]
                if len(keep) != len(lines):
                    open(p, "w").write("\n".join(keep) + "\n")
                    removed += len(lines) - len(keep)
            except Exception:
                pass
    return removed


# ---------------------------------------------------------------- git squash
def squash_history(workdir, msg):
    """把当前工作树压成一个提交并 force push 到 main（历史进 Release，仓库回血）。"""
    def run(cmd):
        return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "main"
    r = run(["git", "checkout", "--orphan", "archive-squash"])
    if r.returncode != 0:
        print("orphan 失败:", r.stderr[:200])
        return False
    run(["git", "add", "-A"])
    r = run(["git", "commit", "-m", msg])
    if r.returncode != 0:
        print("commit 失败:", (r.stdout + r.stderr)[:200])
        run(["git", "checkout", branch])
        return False
    r = run(["git", "push", "-f", "origin", "archive-squash:main"])
    ok = r.returncode == 0
    run(["git", "checkout", branch])
    run(["git", "branch", "-D", "archive-squash"])
    if not ok:
        print("force push 失败:", r.stderr[:300])
    return ok


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", ""))
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    ap.add_argument("--threshold-mb", type=float, default=1024)
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--squash", action="store_true")
    ap.add_argument("--workdir", default=".")
    a = ap.parse_args()
    if not os.path.isdir(a.data):
        print("没有数据目录:", a.data)
        return 1

    data_mb = dir_size(a.data) / 1e6
    git_mb = dir_size(os.path.join(a.workdir, ".git")) / 1e6
    print(f"data/ {data_mb:.1f} MB · .git {git_mb:.1f} MB · 阈值 {a.threshold_mb:.0f} MB")
    stats = {"data_mb": data_mb, "git_mb": git_mb, "keep_days": a.keep_days}
    summary = build_summary(a.data, stats)

    if data_mb < a.threshold_mb and git_mb < a.threshold_mb:
        print("未超阈值，不归档（仍会清理超过 keep-days 的明细）")
        n = prune_old(a.data, a.keep_days)
        print("清理明细:", n)
        return 0

    tag = "data-" + time.strftime("%Y%m%d-%H%M", time.gmtime())
    tar_path = os.path.join("/tmp", f"monitor-data-{tag}.tar.gz")
    make_tar(a.data, tar_path)
    tar_mb = os.path.getsize(tar_path) / 1e6
    print(f"打包 {tar_path}（{tar_mb:.1f} MB）")
    if a.dry_run:
        print(summary)
        return 0
    if not a.token or not a.repo:
        print("缺少 GH_TOKEN / GH_REPO，跳过 Release（本地调试）")
        print(summary)
        return 0

    try:
        st, rel = gh("POST", f"https://api.github.com/repos/{a.repo}/releases", a.token, {
            "tag_name": tag, "name": f"数据归档 {tag}",
            "body": summary, "target_commitish": "main",
            "make_latest": "false",
        })
        print("release:", st, rel.get("html_url"))
        asset_url = f"https://uploads.github.com/repos/{a.repo}/releases/{rel['id']}/assets" \
                    f"?name=monitor-data-{tag}.tar.gz"
        with open(tar_path, "rb") as f:
            st, asset = gh("POST", asset_url, a.token, raw=f.read(),
                           content_type="application/gzip")
        print("asset:", st, asset.get("browser_download_url"))
    except urllib.error.HTTPError as e:
        print("Release 失败:", e.code, e.read().decode()[:300])
        return 1

    # 清理旧明细 + 可选 squash（先清理、再提交、再打包？顺序：先删旧明细 → squash 把删除写进历史）
    n = prune_old(a.data, a.keep_days)
    print("清理明细:", n)
    if a.squash:
        ok = squash_history(a.workdir, f"archive {tag}: squash history（数据见 Release）")
        print("squash:", "OK" if ok else "失败（保留历史，下次再试）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
