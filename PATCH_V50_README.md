# v50 覆盖文件补丁：实时估值固定蛋卷当前页 + 回测历史 PE/PB/ROE

覆盖文件：

```text
app.py
templates/index.html
PATCH_V50_README.md
```

## 改动

1. 设置页中的 PE 来源改为【回测历史估值来源】
   - 只影响历史回测的【历史估值序列】模式。
   - 不再影响实时仓位助手。

2. 实时仓位助手固定使用蛋卷（雪球）当前页面 detail 接口
   - 拉取标的后，PE/PB/ROE/百分位优先使用：
     `https://danjuanfunds.com/djapi/index_eva/detail/{code}`
   - 避免把蛋卷 `pe_history` 的 7 天采样曲线自算结果当成当前页面分位。

3. 历史回测支持蛋卷 PE/PB/ROE 历史曲线
   - PE：`/djapi/index_eva/pe_history/{code}?day=all`
   - PB：`/djapi/index_eva/pb_history/{code}?day=all`
   - ROE：`/djapi/index_eva/roe_history/{code}?day=all`
   - 回测时只使用当日及以前估值数据，避免未来函数。

4. 回测完成后增加估值口径误差记录
   - 在【核心指标】中追加：
     - 历史PE百分位 / 页面PE百分位 / PE百分位误差
     - 历史PB / 页面PB / PB误差
     - 历史PB百分位 / 页面PB百分位 / PB百分位误差
     - 历史ROE / 页面ROE / ROE误差
   - 页面值来自蛋卷 detail 当前页面接口。
   - 误差只用于检查口径差异，不参与交易结果计算。

## 注意

蛋卷 `pe_history/pb_history/roe_history` 是历史曲线接口，可能是图表采样口径；实时仓位页显示的当前分位以 detail 当前页面接口为准。
