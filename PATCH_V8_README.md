# v8：估值多来源轮询 + 未获取提示简化

## 改动

1. PE/PB/ROE 估值获取不再只尝试蛋卷。
   - A股个股：优先 AKShare / 乐咕历史估值，自算 PE/PB 百分位。
   - 指数 / ETF / 联接基金：继续尝试 AKShare 指数估值接口，再尝试蛋卷估值中心兜底。
   - 美股个股：保留 yfinance 当前 PE / ROE；历史 PE 百分位没有稳定来源时不伪造。

2. 自动数据区新增：
   - 当前PB
   - 估值来源
   - 估值提示
   - 数据源尝试记录里包含 valuation:* 估值链路。

3. 未自动获取 PE 百分位时，【估值提示】显示 `--`，不再显示长段失败说明。

4. 修复前端 `renderMetrics` 中错误引用 `data.fetch_trace` 的问题，数据源尝试记录改为显示在自动数据区。

## 运行

```bash
pip install -r requirements.txt
python app.py
```

打开：

```text
http://127.0.0.1:5000
```
