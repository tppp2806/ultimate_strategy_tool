# v40 覆盖文件补丁：回测隐藏字段 + 历史行情源兜底修复

把本补丁里的文件复制到原项目根目录，选择覆盖：

```text
app.py
templates/index.html
static/style.css
```

## 修复 1：历史回测时隐藏实时持仓字段

回测页面现在会隐藏左侧这些实时交易字段：

- 当前持仓
- 当前涨跌幅 / 持仓盈亏 %
- 当前仓位

同时新增了强制 CSS：

```css
[hidden],
.app-shell[data-view="backtest"] [data-hide-in-backtest] {
  display: none !important;
}
```

用于避免 `.field`、`.position-tile` 等 display 样式覆盖 `hidden` 属性，导致字段仍然显示。

## 修复 2：回测历史行情源识别

之前如果顶部【设置】里的数据源是 `akshare`，回测会把它原样传入后端。
但回测历史行情函数只识别 `eastmoney / yahoo / stooq / auto`，所以可能报：

```text
历史回测失败：没有可用的历史行情数据源
```

现在新增 `normalize_backtest_source()`：

- `akshare / danjuan / funddb` → 回测时自动转为 `auto`
- `yfinance / yahoo_chart` → 回测时转为 `yahoo`
- `eastmoney_kline` → 回测时转为 `eastmoney`
- 未知来源 → 回测时转为 `auto`

## 修复 3：国内指数代码 secid / Yahoo 后缀

`000300 / 000016 / 000905 / 000852 / 000688` 这类常见指数虽然以 0 开头，但不是深市股票。
现在回测映射为：

```text
东方财富：1.000300
Yahoo：000300.SS
```

避免被错误映射成：

```text
0.000300
000300.SZ
```

## 建议

如果回测国内指数仍失败，可以先用左侧选择对应 ETF，例如：

```text
510300 沪深300ETF
513100 纳指ETF
```

ETF 的东方财富 K 线通常比指数本身更稳定。
