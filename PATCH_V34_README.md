# v34 回测脚本错误修复覆盖补丁

## 覆盖文件

把本补丁里的文件复制到项目根目录覆盖：

```text
backtest_runner.py
PATCH_V34_README.md
```

## 修复内容

1. 修复交易明细 CSV 写出错误：

```text
ERROR: dict contains fields not in fieldnames: 'realized_pnl', 'realized_return_pct'
```

原因是第一笔交易通常是 BUY，没有 `realized_pnl` / `realized_return_pct` 字段；后续 SELL 行出现这些字段时，`csv.DictWriter` 会报错。现在改为先收集所有行的字段全集，再统一写出。

2. 修复 Python 3.12+ 的时间警告：

```text
DeprecationWarning: datetime.datetime.utcfromtimestamp() is deprecated
```

已改为：

```python
datetime.fromtimestamp(timestamp, datetime.UTC)
```

兼容写法在代码中为：

```python
dt.datetime.fromtimestamp(t, dt.timezone.utc)
```

## 运行

```bash
python backtest_runner.py
```

或指定标的：

```bash
python backtest_runner.py --symbol NVDA --market US --source yahoo --start 2020-01-01 --end 2026-06-06 --position-mode strict_trade
```

如果网络源失败，可以先用本地 CSV：

```bash
python backtest_runner.py --csv data/NVDA.csv --symbol NVDA --start 2020-01-01 --end 2026-06-06
```
