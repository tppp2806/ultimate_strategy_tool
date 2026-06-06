# v48 覆盖文件补丁：历史 PE 百分位来源修正

覆盖文件：

```text
app.py
```

## 改动

1. 【系统自算历史百分位】优先改用当前 AKShare 可用接口：

```python
ak.stock_index_pe_lg(symbol="沪深300")
```

并使用返回表中的【滚动市盈率】作为历史 TTM PE 序列，在本地计算历史 PE 百分位。

2. 新增蛋卷历史 PE 曲线接口：

```text
GET https://danjuanfunds.com/djapi/index_eva/pe_history/{index_code}?day=all
```

解析返回的 `index_eva_pe_growths` 历史 PE 序列后，在本地计算历史 PE 百分位。这个接口可覆盖部分 AKShare/乐咕不支持的指数，例如科创50。

3. 估值来源顺序调整：

- `系统自算历史百分位`：只走 AKShare `stock_index_pe_lg` / 乐咕历史链路，不走蛋卷。
- `蛋卷历史PE优先`：优先用蛋卷 `pe_history` 历史 PE 自算百分位，再用 `detail` 补 PB/ROE。
- `自动`：先系统自算，再蛋卷历史 PE 兜底，最后才尝试当前估值。

4. 回测的【历史估值序列】同样支持蛋卷历史 PE 序列。使用时仍然只取回测当日及以前的估值数据，避免未来函数。

## 说明

- 蛋卷 `/detail` 里的当前 PE 百分位不再作为历史 PE 百分位的优先来源。
- 蛋卷 `/pe_history` 只提供 PE 曲线，PB/ROE 仍需从 `/detail` 或其他来源补充。
- 科创50当前不在 AKShare `stock_index_pe_lg` 支持列表内，建议使用【蛋卷历史PE优先】或【自动】。
