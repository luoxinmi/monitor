# luoxinmi/monitor —— AI 专用行情仓库

**只给 AI agent 用**：内容只有行情快照、保险基金、链上地址与大额转账，无隐私；可自由加删币、
改 workflow、dispatch、清理 `data/`。PAT 在本地 `references/github-credentials.md`，**不进仓库**。

> 本文件是仓库的公开说明；workflow 分档、归档、cron-job.org、gap 口径等运维细节以本地
> `references/github-cloud-ops.md` 为准（单一真相源）。

## 范围

币安 USDT 永续（约 522 个）在各平台（OKX / Bybit / Gate / Bitget / Hyperliquid）与链上的表现。
不是全平台并集（并集 1500+ 币，仓库和 Actions 吃不住）。

## 两个仓库

| 仓库 | 干什么 | 数据 |
|---|---|---|
| `monitor`（本仓库） | 跨平台快照、gap/组基金、watchlist 链上 | `data/<SYM>/`、`data/groups/`、`data/_meta/`、`data/fund/groups_YYYYMM.jsonl` |
| `fund-watch` | ADL 图 16 的基金采样（5 分钟） | `data/fund/YYYYMM.jsonl` |

`monitor.yml` 的 watchlist matrix 当前是 `[EVAA]`；`fund-watch` 采样 `EVAA,ETH,PEPE,BONK`。

## 四档频率

| workflow | cron | 模式 | 做什么 |
|---|---|---|---|
| `live.yml` | `3-59/8 * * * *`（8 分钟） | `fast` | 6 平台行情 + 基金组 + 异动 + 短时爆拉 |
| `live.yml` | `5,35 * * * *`（30 分钟） | `half` | 同上 + 全市场一行 + 基金时序 |
| `universe.yml` | `17 * * * *`（1 小时） | `hour` | 全量 gap + 每币文件夹 |
| `universe.yml` | `23 */4 * * *`（4 小时） | `h4` | 同上 + 强制重写 + 长窗口 180 天 + 归档检查 |
| `monitor.yml` | `0 * * * *`（cron-job.org 保底在 :07） | watchlist | EVAA 深度 + 链上 |

只有 `live.yml` / `universe.yml` 共用 `concurrency: repo-commit`；`monitor.yml` 没有该段。

> **新仓库的 schedule 不触发是 GitHub 的规则（注册时间过短），不是配置问题**，别排查；等放量或手动 dispatch。

## 数据目录（按币名分类）

```
data/<SYM>/venues.json      各平台基本信息（价格/成交额/OI/费率/杠杆/上市时间）
data/<SYM>/gap.json         gap + 组基金 + 组内历史最大缺口 + 长窗口 + squeeze
data/<SYM>/squeeze.json     8m/30m/1h/4h 爆拉缺口
data/<SYM>/gap_daily.jsonl  逐日 gap（保留 120 天）
data/<SYM>/latest.json      watchlist 快照（monitor.py 写）
data/groups/<gid>.json      保险基金组
data/_meta/                 元数据 / 全市场 / 基线 / 基金快照 / 异动
data/fund/groups_YYYYMM.jsonl  组基金时序
```

## 安全

- 仓库只有行情与链上地址，无密钥、无隐私、无本机路径。
- PAT 存在本机 `references/github-credentials.md`，**不写进仓库**。
- 云端抓币安走 `www.binance.com/fapi/...` 透传（runner 直连 451）。
