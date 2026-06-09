from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import (
    _as_optional_pct_value,
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
)
from ..factors import build_mini_factor_result

FAMILY_KEY = "mini_factor_timing"
FAMILY_META: Dict[str, Any] = {
    "order": 30,
    "name": "小因子择时策略",
    "short_name": "小因子",
    "desc": "用趋势、估值、回撤、波动、量能、质量六类轻量因子生成目标仓位；用于验证因子择时是否比纯趋势信号更稳。",
    "status": "研究中",
    "axes": ["趋势", "估值", "回撤", "波动", "量能", "质量"],
}


# -----------------------------------------------------------------------------
# 策略参数微调
# -----------------------------------------------------------------------------
# 与趋势信号策略不同，小因子策略的重点不是“入场按钮”，而是六类因子的权重、
# 手动修正强度和因子失效时的仓位上限。前端会直接读取这些 schema 渲染。
STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "buy_step": 0.22,
        "sell_step": 0.46,
        "risk_multiplier": 1.00,
        "trend_factor_weight_pct": 100.0,
        "valuation_factor_weight_pct": 100.0,
        "drawdown_factor_weight_pct": 100.0,
        "volatility_factor_weight_pct": 105.0,
        "volume_factor_weight_pct": 70.0,
        "quality_factor_weight_pct": 100.0,
        "trend_manual_step_pct": 8.0,
        "structure_manual_step_pct": 6.0,
        "volume_manual_step_pct": 4.0,
        "risk_manual_step_pct": 10.0,
        "bear_cap_pct": 30.0,
        "below200_cap_pct": 46.0,
        "risk_event_cap_pct": 68.0,
        "high_valuation_cap_sideways_pct": 66.0,
        "high_valuation_cap_trend_pct": 78.0,
        "core_base": {"bear": 0.12, "below_200": 0.26, "sideways": 0.50, "above_200": 0.70, "strong_bull": 0.82},
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
        "buy_step": 0.14,
        "sell_step": 0.58,
        "risk_multiplier": 0.70,
        "trend_factor_weight_pct": 85.0,
        "valuation_factor_weight_pct": 125.0,
        "drawdown_factor_weight_pct": 110.0,
        "volatility_factor_weight_pct": 130.0,
        "volume_factor_weight_pct": 60.0,
        "quality_factor_weight_pct": 108.0,
        "trend_manual_step_pct": 5.5,
        "structure_manual_step_pct": 4.5,
        "volume_manual_step_pct": 2.5,
        "risk_manual_step_pct": 12.0,
        "bear_cap_pct": 26.0,
        "below200_cap_pct": 42.0,
        "risk_event_cap_pct": 60.0,
        "high_valuation_cap_sideways_pct": 60.0,
        "high_valuation_cap_trend_pct": 72.0,
        "core_base": {"bear": 0.08, "below_200": 0.20, "sideways": 0.42, "above_200": 0.60, "strong_bull": 0.72},
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
        "buy_step": 0.32,
        "sell_step": 0.36,
        "risk_multiplier": 1.25,
        "trend_factor_weight_pct": 122.0,
        "valuation_factor_weight_pct": 84.0,
        "drawdown_factor_weight_pct": 104.0,
        "volatility_factor_weight_pct": 88.0,
        "volume_factor_weight_pct": 78.0,
        "quality_factor_weight_pct": 118.0,
        "trend_manual_step_pct": 10.0,
        "structure_manual_step_pct": 8.0,
        "volume_manual_step_pct": 5.0,
        "risk_manual_step_pct": 8.0,
        "bear_cap_pct": 36.0,
        "below200_cap_pct": 54.0,
        "risk_event_cap_pct": 76.0,
        "high_valuation_cap_sideways_pct": 72.0,
        "high_valuation_cap_trend_pct": 84.0,
        "core_base": {"bear": 0.18, "below_200": 0.34, "sideways": 0.60, "above_200": 0.80, "strong_bull": 0.90},
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
        "title": "六类因子权重",
        "desc": "小因子策略专属：控制每类因子对目标仓位修正的影响强度。",
        "fields": [
            {"name": "trend_factor_weight_pct", "label": "趋势因子权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "趋势、动量、均线、MACD、RSI、BOLL 的综合权重。"},
            {"name": "valuation_factor_weight_pct", "label": "估值因子权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "PE/PB 百分位对仓位修正的权重。"},
            {"name": "drawdown_factor_weight_pct", "label": "回撤因子权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "近一年回撤修复/回撤吸引力的权重。"},
            {"name": "volatility_factor_weight_pct", "label": "波动因子权重%", "type": "number", "default": 105.0, "min": 0, "max": 200, "step": 1, "tip": "波动、ATR、BOLL 宽度对仓位上限的压制权重。"},
            {"name": "volume_factor_weight_pct", "label": "量能因子权重%", "type": "number", "default": 70.0, "min": 0, "max": 200, "step": 1, "tip": "成交量因子权重；场外基金通常应低于场内 ETF。"},
            {"name": "quality_factor_weight_pct", "label": "质量因子权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "ROE / 盈利质量因子权重。"},
        ],
    },
    {
        "title": "手动修正强度",
        "desc": "当自动行情因子不足，或你有额外判断时，控制手动按钮对仓位的影响幅度。",
        "fields": [
            {"name": "trend_manual_step_pct", "label": "趋势手动修正%", "type": "number", "default": 8.0, "min": 0, "max": 30, "step": 0.5, "tip": "趋势/动量手动修正每次影响的仓位比例。"},
            {"name": "structure_manual_step_pct", "label": "结构手动修正%", "type": "number", "default": 6.0, "min": 0, "max": 30, "step": 0.5, "tip": "结构/回撤手动修正每次影响的仓位比例。"},
            {"name": "volume_manual_step_pct", "label": "量能手动修正%", "type": "number", "default": 4.0, "min": 0, "max": 20, "step": 0.5, "tip": "量能/波动手动修正每次影响的仓位比例。"},
            {"name": "risk_manual_step_pct", "label": "风险手动压低%", "type": "number", "default": 10.0, "min": 0, "max": 40, "step": 0.5, "tip": "额外风险按钮只压低仓位，不提高仓位。"},
        ],
    },
    {
        "title": "因子失效仓位上限",
        "desc": "当大趋势或估值环境不支持因子信号时，限制最高目标仓位。",
        "fields": [
            {"name": "bear_cap_pct", "label": "熊市上限%", "type": "number", "default": 30.0, "min": 0, "max": 100, "step": 0.5, "tip": "市场处于熊市/大空头时的最高仓位。"},
            {"name": "below200_cap_pct", "label": "200日线下上限%", "type": "number", "default": 46.0, "min": 0, "max": 100, "step": 0.5, "tip": "未站上200日线时的最高仓位。"},
            {"name": "risk_event_cap_pct", "label": "风险事件上限%", "type": "number", "default": 68.0, "min": 0, "max": 100, "step": 0.5, "tip": "跌破50日线/突破失败等风险状态下的最高仓位。"},
            {"name": "high_valuation_cap_sideways_pct", "label": "高估震荡上限%", "type": "number", "default": 66.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥90 且非明确多头时的最高仓位。"},
            {"name": "high_valuation_cap_trend_pct", "label": "高估多头上限%", "type": "number", "default": 78.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥90 且趋势仍强时的最高仓位。"},
        ],
    },
]


INPUT_SCHEMA = [
    {
        "title": "② 小因子手动修正",
        "pill": "因子专属",
        "tone": "event-zone",
        "desc": "小因子策略主要依赖自动下载的历史行情因子。这里不是买点按钮，只是在自动因子不足或你有额外判断时做轻量修正。",
        "fields": [
            {"name": "mini_trend_bias", "label": "趋势/动量修正", "type": "select", "default": "", "tip": "修正趋势、60/120日动量、均线斜率这类因子。", "options": [["1", "动量明显偏强"], ["-1", "动量明显偏弱"]]},
            {"name": "mini_structure_bias", "label": "结构/回撤修正", "type": "select", "default": "", "tip": "修正回撤修复、结构突破、距离长期高点等结构因子。", "options": [["1", "结构修复/回撤有吸引力"], ["-1", "结构偏弱"]]},
            {"name": "mini_volume_bias", "label": "量能/波动修正", "type": "select", "default": "", "tip": "修正量能确认、缩量回踩、波动过高等因子。", "options": [["1", "量能/波动支持"], ["-1", "量能/波动不支持"]]},
            {"name": "mini_risk_bias", "label": "风险上限修正", "type": "select", "default": "", "tip": "只用于进一步压低仓位上限，不用于增加仓位。", "options": [["-1", "有额外风险，压低上限"]]},
        ],
    },
]


def _factor_reason(scores: Dict[str, float]) -> str:
    """右侧解释只输出理由，不展示分数/仓位修正/百分比。"""
    positive = []
    negative = []
    neutral = []
    for name in ("趋势", "估值", "回撤", "波动", "量能", "质量"):
        score = float(scores.get(name, 0.0) or 0.0)
        if score >= 0.35:
            positive.append(name)
        elif score <= -0.35:
            negative.append(name)
        else:
            neutral.append(name)
    parts: List[str] = []
    if positive:
        parts.append("正向支持来自" + "、".join(positive))
    if negative:
        parts.append("主要压制来自" + "、".join(negative))
    if not positive and not negative:
        parts.append("各类因子暂未形成明确方向")
    elif neutral:
        parts.append("其余因子影响相对中性")
    return "；".join(parts)


def _manual_bias(signals: Dict[str, Any], key: str) -> int:
    raw = str(signals.get(key, "0") or "0").strip().lower()
    if raw in {"-1", "0", "1"}:
        return int(raw)
    return 0


def _pct_param(strategy: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(strategy.get(key, default)), 0.0, 500.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def _cap_param(strategy: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(strategy.get(key, default)), 0.0, 100.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """小因子择时策略：轻量、可解释、低频优先。"""
    style = get_strategy(cfg)
    notes: List[str] = []
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    pe = _as_optional_pct_value(signals.get("pe_percentile"))

    factor_result = build_mini_factor_result(cfg, signals)
    raw_target = factor_result.raw_target
    manual_adjust = 0.0
    trend_bias = _manual_bias(signals, "mini_trend_bias")
    structure_bias = _manual_bias(signals, "mini_structure_bias")
    volume_bias = _manual_bias(signals, "mini_volume_bias")
    risk_bias = _manual_bias(signals, "mini_risk_bias")
    if trend_bias:
        manual_adjust += trend_bias * _pct_param(style, "trend_manual_step_pct", 8.0)
    if structure_bias:
        manual_adjust += structure_bias * _pct_param(style, "structure_manual_step_pct", 6.0)
    if volume_bias:
        manual_adjust += volume_bias * _pct_param(style, "volume_manual_step_pct", 4.0)
    if risk_bias < 0:
        manual_adjust -= _pct_param(style, "risk_manual_step_pct", 10.0)
    if manual_adjust:
        raw_target = clamp(raw_target + manual_adjust, 0.0, 1.0)

    factor_score_sum = sum(float(v or 0.0) for v in factor_result.scores.values()) + manual_adjust * 10.0
    if raw_target >= 0.68:
        regime = "强因子偏多"
    elif raw_target >= 0.56:
        regime = "因子偏多"
    elif raw_target <= 0.32:
        regime = "强因子偏空"
    elif raw_target <= 0.44:
        regime = "因子偏空"
    else:
        regime = "因子分歧/中性"
    signals["strategy_match_label"] = f"小因子择时：{regime}"
    signals["strategy_confidence"] = int(clamp(58 + abs(factor_score_sum) * 4.0, 55, 86))

    manual_text = "；手动修正进一步压低进攻性" if manual_adjust < 0 else ("；手动修正提高进攻性" if manual_adjust > 0 else "")
    notes.append(f"小因子择时：{regime}，{_factor_reason(factor_result.scores)}{manual_text}。")

    risk_mult = clamp(float(style.get("risk_multiplier", 1.0)), 0.1, 5.0)
    target = 0.50 + (raw_target - 0.50) * clamp(risk_mult, 0.65, 1.35)

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    if market == "bear":
        high = min(high, _cap_param(style, "bear_cap_pct", 30.0))
    elif market == "below_200":
        high = min(high, _cap_param(style, "below200_cap_pct", 46.0))
    elif exit_state in {"below_50", "failed_breakout"}:
        high = min(high, _cap_param(style, "risk_event_cap_pct", 68.0))
    if pe is not None and pe >= 90:
        high = min(high, _cap_param(style, "high_valuation_cap_sideways_pct", 66.0) if market in {"sideways", "below_200", "bear"} else _cap_param(style, "high_valuation_cap_trend_pct", 78.0))

    target = clamp(max(target, floor), low, high)
    return target, notes
