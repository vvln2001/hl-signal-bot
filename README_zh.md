# Hyperliquid 聪明钱信号监听 → Telegram 推送

监听 30 个 Hyperliquid 聪明钱钱包的持仓变化（开多/开空/加仓/减仓/平仓/反手），映射到 OKX USDT 永续合约（`{BASE}-USDT-SWAP`），用中文推送到你的 Telegram。多个 A/B 级钱包 4 小时内同向开同一个币时，额外推送 🔥**共振** 信号。

- 依赖只有 `aiohttp`（Python 3.10+）。实测内存约 25 MB，CPU < 1%，适合 Waifly 免费套餐（300 MB 内存 / 30% CPU）。
- 不需要 Hyperliquid 账号或 API Key，只读公开数据。
- 不会下单，只发提醒。

## 1. 创建 Telegram 机器人

1. 在 Telegram 搜索 **@BotFather**，发送 `/newbot`，按提示设置名字和用户名（必须以 `bot` 结尾）。
2. BotFather 会给你一个 **token**，形如 `123456789:AAH...`。这就是 `TELEGRAM_BOT_TOKEN`，不要泄露。

## 2. 绑定接收人（推荐：只设 token + 发 /start）

**最简单：只设置 `TELEGRAM_BOT_TOKEN`，不设 `TELEGRAM_CHAT_ID`。**

1. 启动机器人程序（见下文）。日志会显示 `TELEGRAM_CHAT_ID not set: waiting for /start ...`。
2. 在 Telegram 里打开你的机器人，**私聊**发送 `/start`。
3. 机器人回复 **“已绑定，开始推送信号”**，之后所有信号只发给你。

说明：
- 只绑定**第一个私聊**发送 `/start` 的人；群组消息、非 `/start` 消息都会忽略。绑定后其他人再发 `/start` 也不会收到任何信号。
- 绑定的 chat id 保存在 `state.json`（字段 `telegram_chat_id`），重启后自动沿用，不用再发 `/start`。
- 想换绑：停止程序，删掉 `state.json` 里的 `telegram_chat_id`（或整个删除 `state.json`，会重新建立持仓基线，不会补发历史信号），再启动并重新发 `/start`。
- 等待绑定期间产生的信号会在队列里排队，绑定后再发出。

**手动方式（可选）**：如果想推送到群组或固定的 chat：先给机器人发一条消息（群组则先把机器人拉进群并在群里发消息），然后运行
```bash
TELEGRAM_BOT_TOKEN=你的token python get_chat_id.py
```
它会打印 `chat_id = ...`，把它设为 `TELEGRAM_CHAT_ID`。设置了 `TELEGRAM_CHAT_ID` 时不会走 /start 自动绑定。
（注意：`get_chat_id.py` 和自动绑定都用 getUpdates，不要和自动绑定同时运行。）

## 3. 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 推送时必填 | 没有 token 时进入 **仅日志模式**：信号打印到控制台并写入 `signals.log` |
| `TELEGRAM_CHAT_ID` | 否 | 不填则用 /start 自动绑定 |
| `MIN_NOTIONAL_USD` | 否 | 最小仓位金额，默认 50000 |
| `ALERT_TIERS` | 否 | 推送的级别，默认 `A,B`（C 级只写日志） |
| `QUIET_MODE` | 否 | `1` = 只推开仓/平仓/反手/共振，加减仓只写日志 |
| `LOG_ONLY` | 否 | `1` = 强制仅日志模式 |

也可以在 `main.py` 同目录放一个 `.env` 文件（推荐在 Waifly 上这样做）：
```
TELEGRAM_BOT_TOKEN=123456789:AAH...
# TELEGRAM_CHAT_ID=可不填
```

其他参数在 `config.json`：`min_notional_usd`、`alert_tiers`、`confluence_window_h`（共振窗口，默认 4 小时）、`confluence_min_wallets`（默认 2）、`change_threshold`（加/减仓阈值，默认 0.20）、`quiet_mode`、`quiet_hours`（例如 `["01:00","08:00"]`，北京时间，此时段消息静音推送）、`max_weight_per_min`（默认 750，Hyperliquid 上限 1200）、`labels`（给钱包起名，`{"0xabc...": "BTC 波段王"}`）、`coin_overrides`（手动映射，例如 `{"xyz:GOLD": "XAU"}`，默认为空：只认完全同名的 OKX 合约）。

## 4. 本地运行

```bash
cd bot
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=你的token     # 不设则仅日志模式
python main.py
```
- 测试：`python -m unittest test_detector test_telegram_bind -v`（含一条模拟信号的完整流程，会打印示例中文消息）。
- 限时运行：`RUN_SECONDS=300 python main.py`（5 分钟后自动退出）。
- 文件：`bot.log`（运行日志，每分钟一行 STATS：权重/分钟、请求数、429 次数、WS 状态、内存）、`signals.log`（所有信号，包括 C 级和被过滤的）、`state.json`（持仓基线、共振记录、绑定的 chat id）。

## 5. 部署到 Waifly（免费）

1. 注册 https://dash.waifly.com ，申请免费套餐，在 Servers 里 **Create**，Egg 选 **Python**。
2. 打开面板 https://panel.waifly.com → 你的服务器 → **Files**，上传 `hl_signal_bot_waifly.zip` 并解压（或用 SFTP 上传整个文件夹的内容）。确保 `main.py`、`requirements.txt`、`config.json`、`data/` 在服务器根目录。
3. 在根目录新建 `.env`，写入 `TELEGRAM_BOT_TOKEN=...`（Waifly 的安全扫描会跳过 `.env`）。如果面板 **Startup** 页有环境变量/启动文件设置：启动文件填 `main.py`；有 “Requirements file” 就填 `requirements.txt`（启动时自动安装依赖）。
4. **Start**。控制台出现 `BASELINE READY ... 30/30` 和 `WS connected` 即正常。若未设 chat id，去 Telegram 私聊机器人发 `/start`。
5. Waifly 免费服务器离线 3 天会被暂停；程序本身崩溃会自动重启，WS 断线会指数退避重连。

## 6. 工作方式

- **混合监控**（与 `smart_wallets.md` 方案一致）：
  - 前 10 个钱包（`monitor = ws_userFills`）：WebSocket 订阅 `userFills`（忽略首个 `isSnapshot` 历史批次）。收到成交后 1.5 秒内拉一次 `clearinghouseState` 做持仓对比；另外每 60 秒 REST 校对一次。
  - 其余 20 个：REST `clearinghouseState` 轮询，所有 REST 请求走同一个节拍器（默认每 160ms 一次 = 750 权重/分钟上限），每个钱包约 3.5 秒刷新一次；遇到 429 暂停 10 秒。
  - HIP-3 股票/商品永续：启动时用 `perpDexs` 获取真实 dex 列表（目前：xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io），对 `hip3_dex_poll=true` 的 15 个钱包扫描全部 dex，之后每 30 秒轮询 `xyz` + 该钱包有持仓的 dex，每 30 分钟重扫一次全部 dex。
  - WS 心跳：每 50 秒发 `ping`；超过 120 秒无消息自动重连。
- **信号**：按钱包+币种对比持仓：开仓（空仓→有仓）、加仓（相对上次提醒的仓位 ≥+20%）、减仓（≤−20% 但未清零）、平仓、反手。分批建仓先小于 $50k 时不提醒，累计超过 $50k 时补发一次“开仓”。
- **启动不刷屏**：首次启动把当前持仓作为基线，不提醒；`state.json` 保存基线，10 分钟内重启会接着对比（只提醒停机期间真实发生的变化），超过 10 分钟则静默重建基线。
- **币种映射**：`kPEPE → PEPE-USDT-SWAP`（k 前缀 = 1000 枚，消息里会注明价格单位）；HIP-3 的 `xyz:NVDA → NVDA-USDT-SWAP`；OKX 没有完全同名 USDT 永续的（如 `xyz:GOLD`、`xyz:SILVER`、`PAXG`）跳过并写日志。
- **Telegram**：发送队列 ≤1 条/秒，失败重试，遇到 429 按 `retry_after` 等待。

## 7. 注意

- 这些钱包是按近期表现挑出来的，存在幸存者偏差；“平仓”也可能只是对冲调整。仓位请按自己的风险管理，不要照抄对方杠杆。
- 同一 IP 上不要再运行其他大量调用 Hyperliquid API 的程序（权重是按 IP 计算的）。
- 定期更新 `data/watchlist.json` / `data/smart_wallets.csv` 后重启即可。
