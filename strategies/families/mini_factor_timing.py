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
        manual_adjust += trend_bias * 0.08
    if structure_bias:
        manual_adjust += structure_bias * 0.06
    if volume_bias:
        manual_adjust += volume_bias * 0.04
    if risk_bias < 0:
        manual_adjust -= 0.10
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
        high = min(high, 0.30)
    elif market == "below_200":
        high = min(high, 0.46)
    elif exit_state in {"below_50", "failed_breakout"}:
        high = min(high, 0.68)
    if pe is not None and pe >= 90:
        high = min(high, 0.66 if market in {"sideways", "below_200", "bear"} else 0.78)

    target = clamp(max(target, floor), low, high)
    return target, notes
