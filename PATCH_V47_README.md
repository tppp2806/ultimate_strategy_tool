# v47 覆盖文件补丁：系统自算 PE 百分位改用乐咕历史 PE 兜底

覆盖文件：

```text
app.py
PATCH_V47_README.md
```

## 背景

你本机 AKShare 1.18.64 里已经没有 `index_value_hist_funddb`，所以系统自算历史 PE 百分位无法通过 AKShare 的 FundDB 历史估值接口完成。

## 修复

1. 保留原 AKShare 尝试链路。
2. 当 AKShare 没有历史 FundDB 接口或返回不足历史序列时，新增直接访问乐咕历史 PE 页面：
   - 沪深300：`https://legulegu.com/stockdata/hs300-ttm-lyr`
   - 上证50：`https://legulegu.com/stockdata/sz50-ttm-lyr`
   - 中证500：`https://legulegu.com/stockdata/zz500-ttm-lyr`
   - 中证800：`https://legulegu.com/stockdata/zz800-ttm-lyr`
   - 上证180：`https://legulegu.com/stockdata/sz180-ttm-lyr`
   - 深证100：`https://legulegu.com/stockdata/sz399330-ttm-lyr`
   - 国证2000：`https://legulegu.com/stockdata/gz2000-ttm-lyr`
   - 创业板50：`https://legulegu.com/stockdata/sz399673-ttm-lyr`
3. 从页面中解析历史 PE 序列，然后在本地按历史序列计算 PE 百分位。
4. 如果仍然失败，日志会显示乐咕 URL、解析条数、失败原因。

## 注意

- 这是“系统自算”，不是蛋卷数据。
- 系统自算必须拿到足够历史 PE 点，少于 30 条仍会判定失败。
- 若乐咕页面结构变化，日志会明确显示“未解析到足够历史 PE”。
