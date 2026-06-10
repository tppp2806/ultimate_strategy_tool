"""简易均线策略。

纯净规则：只读取 ma_position（价格与 20 日均线的关系）。
不读取估值、ROE、大盘趋势、量能、止损、市场风险等其他条件。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import clamp, core_asset_floor_bounds, core_asset_profile, get_strategy, pct2

FAMILY_KEY = "simple_ma"
SIGNAL_DRIVEN = False
FAMILY_META: Dict[str, Any] = {
    "order": 20,
    "name": "简易均线策略",
    "short_name": "简易均线",
    "desc": "只根据价格与 20 日均线的关系给出买入、持有或卖出幅度。",
    "status": "可回测",
    "axes": ["均线"],
}

STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "buy_step": 0.26,
        "sell_step": 0.48,
        "far_below_buy_pct": 30.0,
        "below_buy_pct": 15.0,
        "above_sell_pct": 15.0,
        "far_above_sell_pct": 30.0,
        "trade_step_limit_enabled": True,
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
        "far_below_buy_pct": 18.0,
        "below_buy_pct": 10.0,
        "above_sell_pct": 18.0,
        "far_above_sell_pct": 35.0,
        "trade_step_limit_enabled": True,
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
        "far_below_buy_pct": 40.0,
        "below_buy_pct": 22.0,
        "above_sell_pct": 12.0,
        "far_above_sell_pct": 25.0,
        "trade_step_limit_enabled": True,
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
        "title": "均线策略幅度",
        "desc": "这些参数只属于简易均线策略；策略计算只读取 ma_position，不读取估值、趋势或风险信号。",
        "fields": [
            {"name": "far_below_buy_pct", "label": "远低于买入%", "type": "number", "default": 30.0, "min": 0, "max": 100, "step": 0.5, "tip": "价格远低于 20 日均线时，策略给出的买入幅度。"},
            {"name": "below_buy_pct", "label": "低于买入%", "type": "number", "default": 15.0, "min": 0, "max": 100, "step": 0.5, "tip": "价格低于 20 日均线时，策略给出的买入幅度。"},
            {"name": "above_sell_pct", "label": "高于卖出%", "type": "number", "default": 15.0, "min": 0, "max": 100, "step": 0.5, "tip": "价格高于 20 日均线时，策略给出的卖出幅度。"},
            {"name": "far_above_sell_pct", "label": "远高于卖出%", "type": "number", "default": 30.0, "min": 0, "max": 100, "step": 0.5, "tip": "价格远高于 20 日均线时，策略给出的卖出幅度。"},
        ],
    },
]

INPUT_SCHEMA = [
    {
        "title": "② 均线信号",
        "pill": "均线策略",
        "tone": "neutral",
        "desc": "简易均线策略只使用这个字段。其他估值、趋势、量能、风险输入不会影响该策略。",
        "fields": [
            {
                "name": "ma_position",
                "label": "价格与20日均线关系",
                "type": "choice",
                "default": "at_ma",
                "tip": "价格相对于 20 日均线的位置。",
                "options": [
                    ["far_below", "远低于（>5%）", "buy-strong", "大幅偏离均线，买入幅度更大。"],
                    ["below", "低于（0~5%）", "buy", "价格在均线下方，分批买入。"],
                    ["at_ma", "在均线附近（±1%）", "wait", "维持当前仓位。"],
                    ["above", "高于（0~5%）", "sell", "价格在均线上方，分批减仓。"],
                    ["far_above", "高于（>5%）", "sell-strong", "明显远离均线，卖出幅度更大。"],
                ],
            },
        ],
    },
]


def _pct_param(strategy: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(strategy.get(key, default)), 0.0, 100.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def _current_position(cfg: Dict[str, Any]) -> float:
    try:
        plan = max(float(cfg.get("plan_amount") or 0.0), 0.0)
        amount = max(float(cfg.get("current_position_amount") or 0.0), 0.0)
        return clamp(amount / plan, 0.0, 0.9999) if plan > 0 else 0.0
    except Exception:
        return 0.0


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """只按 ma_position 生成目标仓位。"""
    strategy = get_strategy(cfg)
    cur = _current_position(cfg)
    ma_pos = str(signals.get("ma_position", "at_ma") or "at_ma")

    delta_map = {
        "far_below": (_pct_param(strategy, "far_below_buy_pct", 30.0), "价格远低于 20 日均线，按简易均线规则买入。", "均线深跌买入", 78),
        "below": (_pct_param(strategy, "below_buy_pct", 15.0), "价格低于 20 日均线，按简易均线规则分批买入。", "均线回调买入", 68),
        "at_ma": (0.0, "价格在 20 日均线附近，按简易均线规则维持当前仓位。", "均线附近维持", 56),
        "above": (-_pct_param(strategy, "above_sell_pct", 15.0), "价格高于 20 日均线，按简易均线规则分批卖出。", "均线高位卖出", 66),
        "far_above": (-_pct_param(strategy, "far_above_sell_pct", 30.0), "价格远高于 20 日均线，按简易均线规则加大卖出。", "均线远离卖出", 76),
    }
    delta, note, label, confidence = delta_map.get(ma_pos, delta_map["at_ma"])

    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    target = clamp(cur + delta, low, high)
    actual_delta = target - cur

    signals["strategy_match_label"] = f"简易均线：{label}"
    signals["strategy_confidence"] = int(confidence)

    notes = [note]
    if abs(actual_delta - delta) > 1e-9:
        notes.append(f"仓位边界限制：原始调整 {pct2(delta)}，边界后实际调整 {pct2(actual_delta)}。")
    else:
        notes.append(f"策略原始调整：{pct2(delta)}。")
    return target, notes
