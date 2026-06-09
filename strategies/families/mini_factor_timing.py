from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import (
    _as_optional_pct_value,
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
    pct2,
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
            {"name": "mini_trend_bias", "label": "趋势/动量修正", "type": "select", "default": "auto", "tip": "修正趋势、60/120日动量、均线斜率这类因子。", "options": [["auto", "自动：不手动修正"], ["1", "+1 动量明显偏强"], ["0", "0 动量中性"], ["-1", "-1 动量明显偏弱"]]},
            {"name": "mini_structure_bias", "label": "结构/回撤修正", "type": "select", "default": "auto", "tip": "修正回撤修复、结构突破、距离长期高点等结构因子。", "options": [["auto", "自动：不手动修正"], ["1", "+1 结构修复/回撤有吸引力"], ["0", "0 结构中性"], ["-1", "-1 结构偏弱"]]},
            {"name": "mini_volume_bias", "label": "量能/波动修正", "type": "select", "default": "auto", "tip": "修正量能确认、缩量回踩、波动过高等因子。", "options": [["auto", "自动：不手动修正"], ["1", "+1 量能/波动支持"], ["0", "0 中性"], ["-1", "-1 量能/波动不支持"]]},
            {"name": "mini_risk_bias", "label": "风险上限修正", "type": "select", "default": "auto", "tip": "只用于进一步压低仓位上限，不用于增加仓位。", "options": [["auto", "自动：系统判断"], ["0", "0 无额外风险"], ["-1", "-1 有额外风险，压低上限"]]},
        ],
    },
]

def _manual_bias(signals: Dict[str, Any], key: str) -> int:
    raw = str(signals.get(key, "auto") or "auto").strip().lower()
    if raw in {"-1", "0", "1"}:
        return int(raw)
    return 0


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """小因子择时策略：轻量、可解释、低频优先。

    这不是 Alpha158/360 的替代品，而是接 Qlib 前的验证层：
    - 先确认“因子择时”这个方向是否比纯趋势信号更适合你的基金；
    - 如果小因子策略都无法改善回测，再接上百个因子只会提高过拟合风险；
    - 如果它明显改善，再逐步扩展 Alpha158/360 才有意义。
    """
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
        notes.append(f"手动趋势/动量修正：{trend_bias:+d} -> {pct2(trend_bias * 0.08)}。")
    if structure_bias:
        manual_adjust += structure_bias * 0.06
        notes.append(f"手动结构/回撤修正：{structure_bias:+d} -> {pct2(structure_bias * 0.06)}。")
    if volume_bias:
        manual_adjust += volume_bias * 0.04
        notes.append(f"手动量能/波动修正：{volume_bias:+d} -> {pct2(volume_bias * 0.04)}。")
    if risk_bias < 0:
        manual_adjust -= 0.10
        notes.append("手动风险上限修正：-1 -> -10.00%。")
    if manual_adjust:
        raw_target = clamp(raw_target + manual_adjust, 0.0, 1.0)
        notes.append(f"手动因子修正合计：{pct2(manual_adjust)}，修正后原始目标 {pct2(raw_target)}。")

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
    notes.append(f"小因子基础仓位：{pct2(factor_result.base)}。")
    notes.extend(factor_result.notes)
    notes.append(f"六类因子合计修正：{pct2(factor_result.total_adjustment)}，原始目标仓位 {pct2(raw_target)}。")

    # 参数风格只改变“偏离50%中轴的力度”，不改因子本身的方向。
    risk_mult = clamp(float(style.get("risk_multiplier", 1.0)), 0.1, 5.0)
    target = 0.50 + (raw_target - 0.50) * clamp(risk_mult, 0.65, 1.35)
    notes.append(f"参数风格={style.get('name', '风格')}，风险倍率 {risk_mult:.2f}，风格调整后目标 {pct2(target)}。")

    # 仍然尊重系统防守仓位和仓位模式边界。因子策略不允许在熊市/200日线下无脑满仓。
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
    notes.append(f"风控边界：系统防守仓位 {pct2(floor)}，允许区间 {pct2(low)}~{pct2(high)}。")
    notes.append(f"小因子择时策略最终目标仓位：{pct2(target)}。")
    return target, notes
