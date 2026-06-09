"""简易均线策略。

价格低于 20 日均线时买入，高于 20 日均线一定比例时卖出。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import (
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
    pct2,
)

FAMILY_KEY = "simple_ma"
SIGNAL_DRIVEN = False
FAMILY_META: Dict[str, Any] = {
    "order": 20,
    "name": "简易均线策略",
    "short_name": "简易均线",
    "desc": "价格低于 20 日均线时分批买入，高于 20 日均线一定比例时分批卖出。",
    "status": "可回测",
    "axes": ["均线", "趋势"],
}

STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "buy_step": 0.26,
        "sell_step": 0.48,
        "risk_multiplier": 1.00,
        "sell_above_ma_pct": 5.0,
        "trend_boost_pct": 100.0,
        "core_base": {"bear": 0.14, "below_200": 0.28, "sideways": 0.54, "above_200": 0.72, "strong_bull": 0.84},
        "trade_step_limit_enabled": True,
        "core_step_pct": 22.0,
        "buy_step_limit_pct": 28.0,
        "sell_step_limit_pct": 45.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 60.0,
    },
    "defensive": {
        "buy_step": 0.16,
        "sell_step": 0.60,
        "risk_multiplier": 0.75,
        "sell_above_ma_pct": 3.0,
        "trend_boost_pct": 75.0,
        "core_base": {"bear": 0.10, "below_200": 0.20, "sideways": 0.42, "above_200": 0.58, "strong_bull": 0.70},
        "trade_step_limit_enabled": True,
        "core_step_pct": 13.0,
        "buy_step_limit_pct": 18.0,
        "sell_step_limit_pct": 55.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 60.0,
    },
    "aggressive": {
        "buy_step": 0.36,
        "sell_step": 0.38,
        "risk_multiplier": 1.20,
        "sell_above_ma_pct": 8.0,
        "trend_boost_pct": 130.0,
        "core_base": {"bear": 0.20, "below_200": 0.36, "sideways": 0.64, "above_200": 0.82, "strong_bull": 0.92},
        "trade_step_limit_enabled": True,
        "core_step_pct": 30.0,
        "buy_step_limit_pct": 38.0,
        "sell_step_limit_pct": 35.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 60.0,
    },
}

STYLE_PARAM_SCHEMA: List[Dict[str, Any]] = [
    {
        "title": "均线策略参数",
        "desc": "控制买卖触发条件和趋势权重。",
        "fields": [
            {"name": "sell_above_ma_pct", "label": "卖出阈值%（高于MA）", "type": "number", "default": 5.0, "min": 0, "max": 30, "step": 0.5, "tip": "价格高于 20 日均线多少百分比时开始卖出。"},
            {"name": "trend_boost_pct", "label": "趋势增强%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "整体买入/卖出信号强度倍率。"},
        ],
    },
]

INPUT_SCHEMA = [
    {
        "title": "② 均线信号",
        "pill": "均线策略",
        "tone": "neutral",
        "desc": "输入当前价格与 20 日均线的关系，系统会自动计算目标仓位。",
        "fields": [
            {
                "name": "ma_position",
                "label": "价格与20日均线关系",
                "type": "choice",
                "default": "at_ma",
                "tip": "价格相对于 20 日均线的位置。",
                "options": [
                    ["far_below", "远低于（>5%）", "buy-strong", "大幅偏离均线，较好的买入区间。"],
                    ["below", "低于（0~5%）", "buy", "价格在均线下方，适合分批买入。"],
                    ["at_ma", "在均线附近（±1%）", "wait", "观望为主。"],
                    ["above", "高于（0~5%）", "sell", "接近卖出阈值，考虑减仓。"],
                    ["far_above", "高于（>5%）", "sell-strong", "已超过默认卖出阈值，优先止盈。"],
                ],
            },
        ],
    },
]


def _pct_param(strategy: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(strategy.get(key, default)), 0.0, 500.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """简易均线策略：低于均线买入，高于均线卖出。"""
    strategy = get_strategy(cfg)
    notes: List[str] = []

    ma_pos = str(signals.get("ma_position", "at_ma"))
    sell_threshold = float(strategy.get("sell_above_ma_pct", 5.0)) / 100.0
    trend_boost = _pct_param(strategy, "trend_boost_pct", 100.0)
    risk_mult = float(strategy.get("risk_multiplier", 1.0) or 1.0)

    base_table = strategy.get("core_base") or {}
    base = float(base_table.get("sideways", 0.50))
    target = base

    position_map = {
        "far_below": ("buy-strong", "价格远低于 20 日均线，属于较好的买入区间。"),
        "below": ("buy", "价格低于 20 日均线，适合分批加仓。"),
        "at_ma": ("wait", "价格在均线附近，以观望为主。"),
        "above": ("sell", "价格高于均线，接近卖出阈值。"),
        "far_above": ("sell-strong", "价格已超过卖出阈值，优先止盈。"),
    }

    signal_type, note = position_map.get(ma_pos, ("wait", "未识别的均线位置。"))
    notes.append(note)

    if signal_type == "buy-strong":
        adj = 0.22 * trend_boost
        target += adj
    elif signal_type == "buy":
        adj = 0.12 * trend_boost
        target += adj
    elif signal_type == "sell":
        if sell_threshold > 0:
            adj = -0.10 * risk_mult
        else:
            adj = -0.06 * risk_mult
        target += adj
    elif signal_type == "sell-strong":
        adj = -0.22 * risk_mult
        target += adj
    # "wait" -> no adjustment

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    target = clamp(max(target, floor), low, high)

    regime = {
        "buy-strong": "均线深跌买入",
        "buy": "均线回调加仓",
        "sell": "均线高位减仓",
        "sell-strong": "均线超买止盈",
        "wait": "均线震荡观望",
    }.get(signal_type, "均线震荡")
    signals["strategy_match_label"] = f"简易均线：{regime}"
    signals["strategy_confidence"] = int(clamp(55 + abs(target - 0.50) * 40, 50, 80))
    return target, notes
