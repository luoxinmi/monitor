# monitor

个人用的定时行情监控，每小时跑一次，状态写在 `data/state/`。

- `scripts/monitor.py`：价格 / OI / 费率 / 盘口深度 /（TON 币）链上大额转账，异常记在 `latest.json`
- 数据源自动降级：优先币安，不可用时用 Gate
- 币种配置在 `scripts/coin_profiles.json`
