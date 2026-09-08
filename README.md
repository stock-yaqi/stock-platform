# stock-platform

## 脚本

### scan_minute_volume.py — 分钟级放量扫描

从股票池 CSV 中找出「任一分钟成交量 >= 阈值」的股票，数据来自腾讯 1 分钟 K 线。

```bash
pip install requests pandas
python3 scan_minute_volume.py            # 默认：最新的破净股 CSV，阈值 5 万手，扫最新交易日全天
python3 scan_minute_volume.py -t 30000   # 改阈值
python3 scan_minute_volume.py -i 其他股票池.csv   # 换股票池（需含「代码」列）
python3 scan_minute_volume.py --latest-only      # 只看最新一根分钟 K 线
```

结果输出到 `分钟放量_<日期>.csv`。

### scan_minute_history.py — 分钟放量历史回看

对股票池中每只股票回看近 N 个交易日的分钟 K 线，输出每天的最大分钟量和超阈值分钟数，数据来自新浪 1 分钟 K 线（一次最多约 6 个交易日）。

```bash
python3 scan_minute_history.py                    # 默认：最新的 分钟放量_*.csv，回看 5 个交易日（不含最新一天），阈值 5 万手
python3 scan_minute_history.py -d 5 --include-today
python3 scan_minute_history.py -t 30000 -i 国务院国资委控股_破净股_20260908.csv
```

结果输出到 `分钟放量历史_<日期>.csv`（宽表）和 `分钟放量历史明细_<日期>.csv`（长表）。

### mins_sync.py — A 股全市场 1 分钟 K 线本地库

数据存在 `mins/<股票代码>/<YYYYMMDD>.csv`，每只股票一个目录，每个交易日一个文件，列为
`time,open,high,low,close,volume_hand,amount`（成交量单位手，成交额单位元，09:31 到 15:00 共 240 根）。
主源是通达信行情服务器（可回溯约 4 个多月），北交所和失败的股票走新浪兜底（约 6 个交易日）。

```bash
pip install pytdx requests pandas
python3 mins_sync.py                    # 每日模式：补最近 3 个交易日，非交易日自动跳过
python3 mins_sync.py --days 65 --full   # 回填最近 65 个交易日（约 3 个月）
python3 mins_sync.py --codes 601868 000001
```

日志在 `mins/_logs/`。`mins/` 已加入 .gitignore，不会提交到 git。

### scan_inflow.py — 资金进场扫描

用本地分钟数据找出「成交额突然放大、买盘主动、连续净流入、还没涨完」的股票。指标包括成交额倍数（今日 / 前 20 日均值）、
换手率与换手倍数（流通股本取自腾讯行情）、分钟级净流入占比（分钟涨视为主动买、跌视为主动卖）、近 5 日净流入占比、
连续净流入天数、尾盘 30 分钟成交额占比、最大分钟量倍数，以及 1 / 5 / 20 日涨幅。

```bash
python3 scan_inflow.py                     # 创业板，默认筛选：成交额倍数>=2、净流入为正、近5日净流入为正、20日涨幅<30%、成交额>=1亿
python3 scan_inflow.py --board kcb         # 科创板；zb 主板；bj 北交所；all 全市场
python3 scan_inflow.py --min-ratio 3 --max-gain20 20 --top 30
python3 scan_inflow.py --no-filter         # 输出全部股票指标
```

结果输出到 `资金进场_<板块>_<日期>.csv`，含全部股票的指标和「命中」列。

**定时任务**：launchd 每周一到周五 15:10 运行 `daily_job.sh`（先 `mins_sync.py` 同步分钟数据，再 `scan_inflow.py --board cyb`），配置文件 `~/Library/LaunchAgents/com.qyhdt.stock-mins-sync.plist`。

```bash
launchctl print gui/501/com.qyhdt.stock-mins-sync | head        # 查看状态
launchctl kickstart -k gui/501/com.qyhdt.stock-mins-sync         # 手动立即跑一次
launchctl bootout gui/501/com.qyhdt.stock-mins-sync              # 停用
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.qyhdt.stock-mins-sync.plist   # 重新启用
```

电脑 15:10 处于睡眠时，launchd 会在唤醒后补跑；关机错过的日子由下一次运行的「补最近 3 个交易日」兜底。

### scan_live.py — 盘中「板块共振 + 大单突破」信号（发邮件）

规则来自本地分钟数据回测（1300 笔，次日 10:00 卖平均 +1.33%，胜率 60%；板块不共振的对照组 -0.03%）：

- 板块共振：东财一级行业当日等权涨幅 ≥ 2% 且 ≥ 80% 股票上涨。板块是第一要素。
- 个股：共振板块内，当日涨幅 ≥ 2%，20 日均成交额 ≥ 3 亿。
- 突破：单分钟量 ≥ 当日中位数 8 倍且收盘创当日新高，突破在最近 15 分钟内才提示，每股每天一次。
- 外围过滤：日经 225 或韩国综合当天跌幅 ≥ 1.5% 的日子不出信号（回测：这类日子出现共振板块的概率只有 15%，勉强做平均为负）。`--max-overseas-drop` 调阈值，`--ignore-overseas` 关闭。邮件里附外围快照，隔夜纳指跌超 1.5% 会标注提醒。
- 卖出：次日 10:00 前，早盘冲高即走。次日 09:31 左右自动发卖出提醒邮件。

```bash
python3 scan_live.py --now --no-email      # 立即扫一遍（盘中实时 / 收盘复盘），只打印
python3 scan_live.py --build-cache         # 重建 20 日均额 / 60 日高点缓存（daily_job.sh 已包含）
python3 scan_live.py --test-email
python3 scan_live.py --now --no-email --min-gain 1 --window 30   # 参数：--min-sector --min-up --min-gain --min-amt --spike --window
```

邮件配置写在 `.env`（不进 git）：`SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS SMTP_FROM ALERT_TO`。
信号写入 `信号_<日期>.csv` 和 `mins/_meta/live_signals_<日期>.json`，日志在 `mins/_logs/live_<日期>.log`。

**定时任务**：`~/Library/LaunchAgents/com.qyhdt.stock-live-scan.plist`，每 5 分钟运行一次，脚本自行判断交易时段（周一到周五 09:35-11:30、13:00-14:57）和交易日，命中即发邮件。

```bash
launchctl print gui/501/com.qyhdt.stock-live-scan | head
launchctl bootout gui/501/com.qyhdt.stock-live-scan
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.qyhdt.stock-live-scan.plist
```

### fetch_industry.py — 东财行业分类

拉取全市场东财行业到 `mins/_meta/industry_em.json`，scan_live.py 和 scan_inflow 的板块统计依赖它。新股上市后重跑一次即可，已有的不重复拉。
