# luoxinmi/monitor —— AI 专用行情仓库

**只给 AI agent 用**：内容只有行情快照、保险基金、链上地址与大额转账，无隐私；可自由加删币、
改 workflow、dispatch、清理 `data/`。PAT 在本地 `references/github-credentials.md`，**不进仓库**。

## 云端干什么（用户 2026-11-22 定）

范围 = **币安 USDT 永续（约 522 个）**，追踪它们在各平台（OKX / Bybit / Gate / Bitget / Hyperliquid）
与链上的表现：

| 任务 | 产出 |
|---|---|
| ① 每币跨平台基本信息 | `data/<SYM>/venues.json`（价格 / 24h / 成交额 / OI / 费率 / 最大杠杆 / 上市时间） |
| ② 基金变化 | 币安 124 个保险基金组（**一组共用一个基金池**）+ Gate 全站：`data/_meta/fund.json`、`data/fund/groups_YYYYMM.jsonl`、`data/groups/<gid>.json` |
| ③ 图 16 输入：兑现缺口 + **组内历史最大缺口** | `data/<SYM>/gap.json`（当前 gap、30 天最大、所属组、组内最大、长窗口 180 天） |
| ④ 链上数据 / 链上交易（防对倒） | `data/<SYM>/latest.json`（watchlist 币，`monitor.py`；TON jetton 的 holder / 大额转账 / 余额净变动） |

**时间尺度**：妖币在 8 分钟爆拉、30 分钟爆拉、1 小时持续逼空、4 小时逼空，造成的基金缺口不同。
`data/<SYM>/squeeze.json` 按 **8m / 30m / 1h / 4h** 四个窗口给：被扫空头名义 `N`、穿仓损失 `L`、
相对组基金池的 `uN` / `uL`（1m K 线一次请求覆盖全部窗口，不靠采样历史）。

**历史长度**：币安 `openInterestHist` 只有约 **30 天**（硬上限）；更远用 Gate `contract_stats`
日级（`open_interest_usd`，实测可回 1000 天）→ `gap.json.long_window`（默认 180 天，
`--long-days` 可调，0=关；Gate 单所 OI ≈ 币安 60%，绝对金额偏小、倍数形状可比）。

## 四档频率

| workflow | cron | 模式 | 做什么 |
|---|---|---|---|
| `live.yml` | `3-59/8 * * * *`（8 分钟） | `fast` | 全平台行情 + 基金组 + 异动 + 短时爆拉（top150 按成交额，1m K 线） |
| `live.yml` | `5,35 * * * *`（30 分钟） | `half` | 同上 + 全市场一行 `_meta/market.json` + 基金时序 |
| `universe.yml` | `17 * * * *`（1 小时） | `hour` | 全量 gap（522 币 × 30 天日级 OI + 日线）+ 每币文件夹（变化 ≥5% 才重写） |
| `universe.yml` | `23 */4 * * *`（4 小时） | `h4` | 同上 + 强制重写每币文件 + 组级最大缺口 + 归档检查 |
| `monitor.yml` | `7 * * * *` | watchlist | EVAA/BONK 深度 + 链上（原有） |

三个 workflow 共用 `concurrency: repo-commit`（同仓库只有一个 job 在 push）。

> **新仓库的 schedule 不触发是 GitHub 的规则（注册时间过短），不是配置问题**，别排查；等放量或手动 dispatch。

## 数据目录（按币名分类）

```
data/<SYM>/venues.json       各平台基本信息（+ chain：链上归属）
data/<SYM>/gap.json          当前 gap · 30 天最大 · 所属组 / 组基金 / 组内历史最大缺口 · squeeze
data/<SYM>/squeeze.json      8m/30m/1h/4h 爆拉缺口（被扫空头名义/损失/组基金占比）
data/<SYM>/gap_daily.jsonl   逐日 gap（保留最近 120 天）
data/<SYM>/latest.json       旧版 watchlist 快照（monitor.py；gh_snapshot.py 兼容读）
data/groups/<gid>.json       保险基金组：基金池 / 成员 / 组内最大缺口 / 组内最大短时缺口
data/groups/index.json       组清单
data/_meta/{scan,market,live,fund}.json     本轮元数据 / 全市场一行 / 基线 / 基金快照
data/_meta/alerts/YYYYMMDD.jsonl            异动（价格±15% / OI±10% / 费率≥0.1% / 新上市 / 组基金±5% / 爆拉）
data/fund/groups_YYYYMM.jsonl               组基金时序（每小时最多一行/组）
```

## 存储：超阈值自动打包 Release + squash

Public 仓库的 **Releases 不限量**（单 asset ≤ 2GB），仓库 git 历史会膨胀 → `scripts/archive.py`：
`data/` 或 `.git` 超过 1024MB 时，把 `data/` 打包成 tar.gz 发 Release（tag `data-YYYYMMDD-HHMM`），
**Release 说明写关键数据**（各币在组内造成的最大基金缺口 Top 20、组基金池 Top 10、扫描概况），
清理 30 天前的明细，再把工作树压成一个提交 force push —— 仓库回血，历史数据在 Release 里。

## 加币 / 改范围

- 默认清单 = 币安 USDT 永续（自动发现，新上市自动纳入）。
- 链上只覆盖配了 `network/jetton` 的 TON 币（`scripts/coin_profiles.json`，tonapi 免费）；
  其他链要付费 key，`venues.json` 对未配置的币不写 `chain`。
- 想扩到全平台并集：`universe_scan.py --union`（1500+ 币，仓库和数据量都大，慎用）。

## 本地用法

```bash
python3 scripts/universe_scan.py --out /tmp/u --proxy http://127.0.0.1:7890 --mode hour --limit 20
python3 scripts/archive.py --data data --dry-run          # 只看摘要，不发布
```

`--mode fast|half|hour|h4|full`、`--limit N`、`--only BTC,ETH`、`--no-gap`、`--days 30`、`--concurrency 10`。
