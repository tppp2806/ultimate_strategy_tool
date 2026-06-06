# V10 补丁：蛋卷 JSON 接口 + 设置页连通性测试

## 改动

1. 蛋卷估值改为 JSON 接口：

```text
GET https://danjuanfunds.com/djapi/index_eva/detail/{指数代码}
```

例如：

```text
/djapi/index_eva/detail/SH000300
```

2. 设置页新增「蛋卷 Cookie（可选）」：
   - 不再把 Cookie 写死在代码里。
   - 如果蛋卷接口需要 Cookie，可以从 Apifox/浏览器复制后粘贴。

3. 顶部新增「设置」按钮：
   - 数据源顺序、代理模式、代理地址、超时、重试、蛋卷 Cookie 都移到设置页。
   - 左侧资金区不再显示数据源容错配置。

4. 设置页新增「测试接口连通性」：
   - yahoo_chart
   - yfinance
   - danjuan_json
   - stooq_csv
   - eastmoney_kline

测试结果会显示：

- 是否成功
- URL
- 状态码
- 耗时
- 行数/条数
- 解析结果
- 响应预览或错误

5. 当前标的点击可展开/收起搜索栏，带动画过渡。

## 新接口

```text
POST /api/connectivity-test
```

请求体示例：

```json
{
  "symbol": "510300",
  "market": "CN",
  "asset_kind": "etf",
  "proxy_mode": "system",
  "proxy_url": "",
  "request_timeout_sec": 12,
  "retry_count": 2,
  "danjuan_cookie": ""
}
```
