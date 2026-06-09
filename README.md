# strategies 策略目录

这里放“总体策略”，不是防守/均衡/进攻这种参数风格。

## 当前结构

```text
strategies/
├─ strategy_engine.py          # 兼容入口：app.py 只导入这里
├─ registry.py                 # 自动发现总体策略 / 分发器
├─ base.py                     # 公共参数风格、工具函数、执行层辅助函数
└─ families/                   # 每个总体策略一个独立 py
   ├─ trend_signal_control.py  # 趋势信号风控策略
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

def target_weight(cfg, signals):
    # 返回：目标仓位 0~1、解释列表
    return 0.50, ["示例：目标仓位50%。"]
```

4. 重启 Flask。

不需要改 `registry.py`，不需要改 HTML，顶部会自动出现。

## 移除一个总体策略

删除对应的 `strategies/families/xxx.py`，重启 Flask 即可。

## 排序

`FAMILY_META["order"]` 越小，顶部卡片越靠前。

## 小因子择时策略

新增文件：

```text
families/mini_factor_timing.py
factors/mini_factors.py
```

`mini_factor_timing.py` 是总体策略入口，`mini_factors.py` 负责计算可解释小因子。后续如果要接 Qlib Alpha158/Alpha360，建议继续放在 `strategies/factors/` 下，不要把大量因子计算塞进 `app.py` 或具体策略文件。
