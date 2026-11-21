# GitHub Actions 定时监控（通用）

用 GitHub 的免费 runner 每 30 分钟跑一次监控：币安价格/OI/费率/盘口 +（TON 币）链上大额转账，
异常只记在 `latest.json` 的 `alerts` 字段里（可选推 Discord webhook）；**不自动开 issue**，避免仓库里堆积可见通知。

**分工约定（2026-11-21 用户定）**：
- **GitHub Actions 只跑 Gate + 链上**：runner 在美国，币安 451、Bybit 403，所以 workflow 用 `--no-binance`
  （Gate 的 tickers/contract_stats/order_book 全球可用，tonapi 直连可用）；
- **币安等平台数据留在本地 DSH 分析**（本机有代理），不在 GitHub 上折腾代理。

**加币步骤（三步）**：
1. `scripts/coin_profiles.json` 加一段（`binance`/`gate` 自动推导，有 TON jetton 才填 `jetton`）；
2. `monitor.yml` 的 `matrix.symbol` 加名字（如 `[EVAA, BTC, BONK]`）；
3. 推送即可；每个币独立 `data/<SYM>/state.json`，脚本链上部分自动跳过非 TON 币。

**为什么放 GitHub Actions**：runner 在海外，币安/Bybit/tonapi 都直连，不需要本机代理；
不占子代理、不占主会话 token；仓库里的 `data/state.json` 就是监控历史。

## 你只需要做三件事

1. **新建一个仓库**（推荐 **Public**：GitHub Actions 免费不限分钟、无额度焦虑；仓库里只有
   价格/OI/费率/盘口/链上地址快照这类行情数据，不涉及隐私）。用 Private 也行（每月 2000 分钟，
   30 分钟一次约 1440 分钟/月）。
2. **上传两个文件**（保持目录结构）：
   ```
   scripts/monitor.py
   .github/workflows/monitor.yml
   ```
   `monitor.py` 从本技能目录复制（`$DSH_HOME/skills/liq-heatmap/scripts/monitor.py`）。
3. **启用 Actions**：仓库 → Actions 标签 → 允许 workflow → 跑一次 `workflow_dispatch` 看结果。

可选：仓库 Settings → Secrets → 新建 `WEBHOOK_URL`（Discord/Slack/Telegram 的 webhook），
异常时会额外推一条消息。

## 它会报什么异常

- 价格较上次变化 ≥5%
- OI 1h 变化 ≥10%
- 资金费率绝对值 ≥0.1%/8h
- ±1% 买盘深度变化 ≥30%
- 链上新增大额转账（≥3000 EVAA，48 小时内，按 event_id 去重）

每次运行都会 commit `data/state.json` 和 `data/latest.json`，异常时开 issue。

## 限制（别指望它实时）

- GitHub schedule **最小 5 分钟、实际常延迟几分钟到十几分钟**；30 分钟一跑是稳妥值。
- 仓库 **60 天无活动** 会自动暂停 schedule——本工作流每次 commit，正好保持活跃。
- **公开仓库 Actions 免费不限分钟**；私有仓库超 2000 分钟/月会收费。
- 数据（state/latest）会进仓库：公开仓库里任何人可看这些行情快照（价格/OI/费率/盘口/链上地址），
  确认无隐私顾虑即可——这些数据只对监控本身有价值。
- 首次运行只建立基线，不会误报历史转账。

## 本地跑（同一份脚本）

```bash
python3 /root/.dsh/skills/liq-heatmap/scripts/monitor.py --symbol EVAAUSDT --out /tmp/monitor --proxy http://127.0.0.1:7890
```
