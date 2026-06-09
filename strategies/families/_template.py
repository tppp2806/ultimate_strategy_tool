"""总体策略模板。

复制本文件为 my_strategy.py，并删除文件名前面的下划线，重启 Flask 后就会自动出现在顶部总体策略卡片中。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import clamp, core_asset_floor_bounds, core_asset_profile, get_strategy, lower_floor, pct2

FAMILY_KEY = "my_strategy"
FAMILY_META: Dict[str, Any] = {
    "order": 90,
    "name": "我的策略",
    "short_name": "我的策略",
    "desc": "用一句话说明这套总体策略的买卖逻辑。",
    "status": "研究中",
    "axes": ["估值", "趋势"],
}

# 可编辑参数也写在策略 Python 里；registry 会把它交给前端，前端只负责渲染。
STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "defensive": {"buy_step": 0.16, "sell_step": 0.58, "risk_multiplier": 0.75, "my_signal_weight_pct": 80.0, "core_base": {"bear": 0.10, "below_200": 0.20, "sideways": 0.42, "above_200": 0.58, "strong_bull": 0.70}, "trade_step_limit_enabled": True, "core_step_pct": 13.0, "buy_step_limit_pct": 18.0, "sell_step_limit_pct": 55.0, "core_min_position_pct": 5.0, "core_max_position_pct": 92.0, "strict_min_position_pct": 0.0, "strict_max_position_pct": 60.0},
    "balanced": {"buy_step": 0.26, "sell_step": 0.46, "risk_multiplier": 1.00, "my_signal_weight_pct": 100.0, "core_base": {"bear": 0.14, "below_200": 0.28, "sideways": 0.54, "above_200": 0.72, "strong_bull": 0.84}, "trade_step_limit_enabled": True, "core_step_pct": 22.0, "buy_step_limit_pct": 28.0, "sell_step_limit_pct": 45.0, "core_min_position_pct": 5.0, "core_max_position_pct": 92.0, "strict_min_position_pct": 0.0, "strict_max_position_pct": 60.0},
    "aggressive": {"buy_step": 0.36, "sell_step": 0.36, "risk_multiplier": 1.25, "my_signal_weight_pct": 120.0, "core_base": {"bear": 0.20, "below_200": 0.36, "sideways": 0.64, "above_200": 0.82, "strong_bull": 0.92}, "trade_step_limit_enabled": True, "core_step_pct": 30.0, "buy_step_limit_pct": 38.0, "sell_step_limit_pct": 35.0, "core_min_position_pct": 5.0, "core_max_position_pct": 92.0, "strict_min_position_pct": 0.0, "strict_max_position_pct": 60.0},
}

STYLE_PARAM_SCHEMA: List[Dict[str, Any]] = [
    {
        "title": "我的策略专属参数",
        "desc": "新增策略只需声明专属参数；执行速度、仓位边界等全局参数已移至左侧统一配置。",
        "fields": [
            {"name": "my_signal_weight_pct", "label": "信号权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "示例字段：在 target_weight 中通过 get_strategy(cfg) 读取。"},
        ],
    },
    {
        "title": "执行层控制",
        "desc": "控制单次买入/卖出/补仓的操作上限。调到 100% 等于不限制。",
        "fields": [
            {"name": "core_step_pct", "label": "补仓上限%", "type": "number", "default": 22.0, "min": 0, "max": 100, "step": 0.1, "tip": "定投增强策略每个检查日最多补多少定投增强仓位。"},
            {"name": "buy_step_limit_pct", "label": "买入上限%", "type": "number", "default": 28.0, "min": 0, "max": 100, "step": 0.1, "tip": "纯交易仓/普通买入信号的单次买入上限。"},
            {"name": "sell_step_limit_pct", "label": "卖出上限%", "type": "number", "default": 45.0, "min": 0, "max": 100, "step": 0.1, "tip": "基础单次卖出上限；严重破位时仍会按风险倍数放大。"},
        ],
    },
]


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """返回目标仓位 0~1 和解释文本列表。"""
    style = get_strategy(cfg)
    notes: List[str] = []

    # 示例：先以 50% 为中性仓位，再按参数风格的风险倍率与策略专属字段微调。
    risk_mult = float(style.get("risk_multiplier", 1.0) or 1.0)
    signal_weight = float(style.get("my_signal_weight_pct", 100.0) or 100.0) / 100.0
    target = 0.50 + (risk_mult - 1.0) * 0.08 + (signal_weight - 1.0) * 0.05

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    target = clamp(max(target, floor), low, high)

    notes.append(f"模板策略：参数风格={style.get('name', '风格')}，目标仓位 {pct2(target)}。")
    return target, notes
