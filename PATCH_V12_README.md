# v12 修复：yfinance 空数据诊断 + 东方财富 HTTPS/Referer + 国内行情兜底

## 改动

1. yfinance 连通性测试增强
   - 先尝试 `yf.download(..., multi_level_index=False)`。
   - 兼容旧版 yfinance：不支持该参数时自动退回普通 `yf.download`。
   - 如果 `download` 返回空表，再尝试 `yf.Ticker(symbol).history(...)`。
   - 测试结果会显示实际使用的方法：`download` 或 `Ticker.history`。

2. 东方财富 K 线接口修复
   - 默认从 `http://push2his...` 改为 `https://push2his...`。
   - 添加更完整的请求头：`User-Agent`、`Accept`、`Referer: https://quote.eastmoney.com/`。
   - 连通性测试中会先试 HTTPS，失败再试 HTTP，并在解析结果里显示 `tried` 记录。

3. 国内行情新增直接东方财富兜底链路
   - `AKShare` 失败时，自动尝试 `eastmoney_kline`。
   - 主要用于 A 股、ETF、场内基金的 OHLCV 数据获取。

## 仍需注意

- `yfinance` 本身依赖 Yahoo 非官方链路。若 `yahoo_chart` 正常但 `yfinance` 失败，程序会优先使用更直接的 `yahoo_chart` 行情链路。
- 东方财富如果返回 502，常见原因是 HTTP 链路、请求头、代理或临时网关异常；v12 已改为 HTTPS + 常规 Referer，并自动 HTTP 兜底。
