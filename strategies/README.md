# strategies 策略目录

这里放“总体策略”，不是防守/均衡/进攻这种参数风格。

## 当前结构

```text
strategies/
├─ strategy_engine.py          # 兼容入口：app.py 只导入这里
├─ registry.py                 # 自动发现总体策略 / 分发器 / 参数 schema 暴露
├─ base.py                     # 公共参数风格、工具函数、执行层辅助函数
└─ families/                   # 每个总体策略一个独立 py
   ├─ trend_signal_control.py  # 趋势信号风控策略
   ├─ mini_factor_timing.py    # 小因子择时策略
   ├─ five_dimension_timing.py # 已移除占位，不会注册
   └─ _template.py             # 策略模板；以下划线开头，不会自动加载
```

## 前端怎么切换策略

顶部会根据 `strategies/families/*.py` 自动生成总体策略卡片：

```text
总体策略卡片
├─ 点击卡片主体：切换总体策略
└─ 点击右侧【参数】：编辑该总体策略自己的参数风格微调
```

HTML 不手写具体策略名称，因此新增 / 移除策略时，前端布局会自动跟随。

## 策略参数微调从哪里来

每个总体策略文件可以声明：

```python
STYLE_PARAM_PRESETS = {
    "defensive": {...},
    "balanced": {...},
    "aggressive": {...},
}

STYLE_PARAM_SCHEMA = [
    {"title": "执行速度", "fields": [...]},
    {"type": "core_base_table", "name": "core_base_pct", "title": "基础仓位表"},
]
```

`registry.py` 会把这两个对象放进 `STRATEGY_FAMILIES`。前端只根据 schema 渲染，不再硬编码“均衡微调”里的字段。后端 `get_strategy(cfg)` 会把当前总体策略 + 当前参数风格对应的可编辑参数一起返回给 `target_weight` 使用。

## 新增一个总体策略

1. 复制：

```text
strategies/families/_template.py
```

2. 改名，例如：

```text
strategies/families/grid_valuation.py
```

3. 修改文件里的：

```python
FAMILY_KEY = "grid_valuation"
FAMILY_META = {
    "order": 30,
    "name": "网格估值策略",
    "short_name": "网格估值",
    "desc": "说明这个策略怎么判断买卖。",
    "status": "研究中",
    "axes": ["估值", "仓位"],
}

STYLE_PARAM_PRESETS = {...}
STYLE_PARAM_SCHEMA = [...]

def target_weight(cfg, signals):
    style = get_strategy(cfg)
    # style 中包含 STYLE_PARAM_SCHEMA 声明的字段
    return 0.50, ["示例：目标仓位50%。"]
```

4. 重启 Flask。

不需要改 `registry.py`，不需要改 HTML，顶部会自动出现。

## 移除一个总体策略

删除对应的 `strategies/families/xxx.py`，重启 Flask 即可。

## 排序

`FAMILY_META["order"]` 越小，顶部卡片越靠前。
