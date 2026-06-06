# v13：映射表外置

## 改动

1. 新增外置映射表：

```text
data/index_map.json
```

用于维护：

- 常用标的搜索候选 `local_symbols`
- 名称/关键词搜索别名 `aliases`
- 指数代码映射 `index_codes`
- ETF/联接基金 → 跟踪指数映射 `fund_index_map`
- AKShare 指数中文名映射 `index_names`
- 名称关键词 → 指数代码规则 `keyword_rules`

2. 新增接口：

```http
GET  /api/index-map
POST /api/index-map
```

保存后会热更新，无需重启应用。

3. 顶部【设置】页新增“指数 / 基金映射表”编辑区：

- 载入映射表
- 保存并热更新
- 显示映射数量

## 使用示例

给某只基金新增跟踪指数映射：

```json
"fund_index_map": {
  "510300": "SH000300",
  "你的基金代码": "SH000300"
}
```

新增关键词识别：

```json
"keyword_rules": [
  {"keywords": ["红利低波", "中证红利低波"], "index_code": "SHxxxxxx"}
]
```

注意：基金/ETF 自己通常没有 PE 百分位，脚本会通过这张表映射到跟踪指数后再获取估值。
