# v33 历史回测覆盖补丁

## 覆盖文件

把本补丁里的文件复制到项目根目录：

```text
backtest_runner.py
BACKTEST_V33_README.md
```

这次不改前端、不改主程序，只新增一个独立回测脚本。

## 回测原则

1. 第 `t` 日收盘后，用截至当天的历史数据计算信号。
2. 第 `t+1` 日开盘执行交易，避免使用未来数据。
3. 默认不使用 PE/ROE 历史估值，因为当前系统拿到的 PE/ROE 很多是“当前值”，直接回填历史会产生前视偏差。
4. 如果你手动传入 `--pe-percentile`、`--roe-pct`，脚本会按固定值参与计算，但报告会注明这是固定估值输入，不是真正逐日历史估值。
5. 输出策略和买入持有基准的收益、年化、回撤、波动、Sharpe、交易次数、胜率、profit factor、平均仓位和换手。

## 运行示例

### 美股：NVDA

```bash
python backtest_runner.py --symbol NVDA --market US --source yahoo --start 2020-01-01 --end 2026-06-06 --position-mode strict_trade
```

### 美股ETF：QQQ

```bash
python backtest_runner.py --symbol QQQ --market US --source yahoo --start 2020-01-01 --end 2026-06-06 --position-mode core_satellite
```

### A股ETF：510300

```bash
python backtest_runner.py --symbol 510300 --market CN --source eastmoney --start 2020-01-01 --end 2026-06-06 --position-mode core_satellite
```

### 本地CSV

CSV 字段支持：

```text
Date,Open,High,Low,Close,Volume
```

或：

```text
date,open,high,low,close,volume
```

运行：

```bash
python backtest_runner.py --csv data/NVDA.csv --symbol NVDA --market US --start 2020-01-01 --end 2026-06-06
```

## 输出文件

默认输出到：

```text
backtest_reports/
```

包含：

```text
*_report.md       回测报告
*_equity.csv      每日权益曲线
*_trades.csv      交易明细
*_metrics.json    核心指标 JSON
```

## 真实回测的限制

这是真实日线历史行情回测，但仍有局限：

- 不包含逐日真实 PE/PB/ROE，除非后续接入历史估值序列。
- 对场外基金/QDII联接基金，成交价和净值确认机制不同，日线回测只能近似。
- 滑点、手续费默认简化为固定 bps。
- 当前策略不是为高频/日内交易设计，只适合日线/波段级别验证。

## 建议测试矩阵

至少跑：

```bash
python backtest_runner.py --symbol QQQ --market US --source yahoo --position-mode core_satellite --start 2020-01-01
python backtest_runner.py --symbol SPY --market US --source yahoo --position-mode core_satellite --start 2020-01-01
python backtest_runner.py --symbol NVDA --market US --source yahoo --position-mode strict_trade --start 2020-01-01
python backtest_runner.py --symbol 510300 --market CN --source eastmoney --position-mode core_satellite --start 2020-01-01
```
