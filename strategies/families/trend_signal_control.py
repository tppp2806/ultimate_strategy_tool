from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import (
    STRATEGY_PRESETS,
    _as_optional_pct_value,
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
)

FAMILY_KEY = "trend_signal_control"
FAMILY_META: Dict[str, Any] = {
    "order": 10,
    "name": "趋势信号风控策略",
    "short_name": "趋势信号",
    "desc": "以趋势状态为主轴，结合突破/回踩/破位、估值、ROE和风险事件生成目标仓位。",
    "status": "可实盘/可回测",
    "axes": ["趋势", "信号", "估值", "质量", "风控"],
}


# -----------------------------------------------------------------------------
# 策略参数微调
# -----------------------------------------------------------------------------
# 这些字段由 registry 暴露给前端；前端只根据 schema 渲染，不再硬编码参数项。
# 数值单位：字段名以 _pct 结尾的参数在 UI 中显示为百分数，策略里按需 /100 使用。
STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "defensive": {
        "buy_step": 0.16,
        "sell_step": 0.60,
        "risk_multiplier": 0.75,
        "entry_bonus_pct": 70.0,
        "risk_penalty_pct": 122.0,
        "valuation_sensitivity_pct": 112.0,
        "quality_weight_pct": 78.0,
        "bear_cap_pct": 22.0,
        "below200_cap_pct": 38.0,
        "high_valuation_cap_sideways_pct": 62.0,
        "high_valuation_cap_trend_pct": 76.0,
        "extreme_valuation_cap_sideways_pct": 52.0,
        "extreme_valuation_cap_trend_pct": 68.0,
        "core_base": {"bear": 0.08, "below_200": 0.18, "sideways": 0.38, "above_200": 0.58, "strong_bull": 0.72},
        "trade_step_limit_enabled": True,
        "core_step_pct": 13.0,
        "buy_step_limit_pct": 18.0,
        "sell_step_limit_pct": 55.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 60.0,
    },
    "balanced": {
        "buy_step": 0.26,
        "sell_step": 0.48,
        "risk_multiplier": 1.00,
        "entry_bonus_pct": 100.0,
        "risk_penalty_pct": 100.0,
        "valuation_sensitivity_pct": 100.0,
        "quality_weight_pct": 100.0,
        "bear_cap_pct": 24.0,
        "below200_cap_pct": 42.0,
        "high_valuation_cap_sideways_pct": 68.0,
        "high_valuation_cap_trend_pct": 80.0,
        "extreme_valuation_cap_sideways_pct": 58.0,
        "extreme_valuation_cap_trend_pct": 72.0,
        "core_base": {"bear": 0.12, "below_200": 0.26, "sideways": 0.52, "above_200": 0.72, "strong_bull": 0.84},
        "trade_step_limit_enabled": True,
        "core_step_pct": 22.0,
        "buy_step_limit_pct": 28.0,
        "sell_step_limit_pct": 45.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 60.0,
    },
    "aggressive": {
        "buy_step": 0.36,
        "sell_step": 0.38,
        "risk_multiplier": 1.20,
        "entry_bonus_pct": 128.0,
        "risk_penalty_pct": 86.0,
        "valuation_sensitivity_pct": 88.0,
        "quality_weight_pct": 120.0,
        "bear_cap_pct": 30.0,
        "below200_cap_pct": 50.0,
        "high_valuation_cap_sideways_pct": 74.0,
        "high_valuation_cap_trend_pct": 86.0,
        "extreme_valuation_cap_sideways_pct": 64.0,
        "extreme_valuation_cap_trend_pct": 78.0,
        "core_base": {"bear": 0.18, "below_200": 0.34, "sideways": 0.62, "above_200": 0.82, "strong_bull": 0.92},
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
        "title": "执行速度",
        "desc": "控制本策略从当前仓位靠近目标仓位的速度。",
        "fields": [
            {"name": "buy_step_pct", "label": "买入节奏%", "type": "number", "default": 26.0, "min": 0, "max": 100, "step": 0.1, "tip": "买入/加仓时的单次执行速度。越高越快接近目标仓位。"},
            {"name": "sell_step_pct", "label": "卖出节奏%", "type": "number", "default": 48.0, "min": 0, "max": 100, "step": 0.1, "tip": "减仓/止盈时的单次执行速度。越高卖出越快。"},
            {"name": "risk_multiplier", "label": "风险倍率", "type": "number", "default": 1.0, "min": 0.1, "max": 5, "step": 0.05, "tip": "风险预算倍率。1=默认，低于1更保守，高于1更激进。"},
        ],
    },
    {
        "title": "趋势信号权重",
        "desc": "趋势信号策略专属：控制入场信号、风险信号、估值和质量对目标仓位的影响强度。",
        "fields": [
            {"name": "entry_bonus_pct", "label": "入场信号强度%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "突破、回踩不破、强趋势延续等入场信号的加仓幅度倍率。"},
            {"name": "risk_penalty_pct", "label": "风险信号惩罚%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "破位、冲高回落、大盘同步走弱等风险信号的减仓幅度倍率。"},
            {"name": "valuation_sensitivity_pct", "label": "估值敏感度%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "PE/PB 百分位对目标仓位的影响倍率。"},
            {"name": "quality_weight_pct", "label": "盈利质量权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "ROE 对目标仓位的影响倍率；高估时仍会自动衰减。"},
        ],
    },
    {
        "title": "仓位上限",
        "desc": "趋势策略的硬风控上限。用于限制熊市、200日线下和高估环境的最高目标仓位。",
        "fields": [
            {"name": "bear_cap_pct", "label": "熊市上限%", "type": "number", "default": 24.0, "min": 0, "max": 100, "step": 0.5, "tip": "大趋势进入熊市/大空头时的最高仓位。"},
            {"name": "below200_cap_pct", "label": "200日线下上限%", "type": "number", "default": 42.0, "min": 0, "max": 100, "step": 0.5, "tip": "未站上200日线时的最高仓位。"},
            {"name": "high_valuation_cap_sideways_pct", "label": "高估震荡上限%", "type": "number", "default": 68.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥90 且非明确多头时的最高仓位。"},
            {"name": "high_valuation_cap_trend_pct", "label": "高估多头上限%", "type": "number", "default": 80.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥90 且趋势仍强时的最高仓位。"},
            {"name": "extreme_valuation_cap_sideways_pct", "label": "极高估震荡上限%", "type": "number", "default": 58.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥95 且非明确多头时的最高仓位。"},
            {"name": "extreme_valuation_cap_trend_pct", "label": "极高估多头上限%", "type": "number", "default": 72.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE≥95 且趋势仍强时的最高仓位。"},
        ],
    },
    {
        "title": "执行层控制",
        "desc": "控制目标仓位和执行节奏。想直接打到目标仓位，可以关闭【启用单次操作上限】，或把对应单次上限调到 100%。",
        "fields": [
            {"name": "trade_step_limit_enabled", "label": "启用单次操作上限", "type": "checkbox", "default": True, "tip": "关闭后，检查日会直接调到策略目标仓位；仍保留操作周期、最小执行变化、手续费和滑点。"},
            {"name": "core_step_pct", "label": "补仓上限%", "type": "number", "default": 22.0, "min": 0, "max": 100, "step": 0.1, "tip": "定投增强策略每个检查日最多补多少定投增强仓位。"},
            {"name": "buy_step_limit_pct", "label": "买入上限%", "type": "number", "default": 28.0, "min": 0, "max": 100, "step": 0.1, "tip": "纯交易仓/普通买入信号的单次买入上限。"},
            {"name": "sell_step_limit_pct", "label": "卖出上限%", "type": "number", "default": 45.0, "min": 0, "max": 100, "step": 0.1, "tip": "基础单次卖出上限；严重破位时仍会按风险倍数放大。"},
        ],
    },
    {
        "title": "目标仓位边界",
        "desc": "控制策略目标仓位的最低和最高边界。",
        "fields": [
            {"name": "core_min_position_pct", "label": "增强最低仓位%", "type": "number", "default": 5.0, "min": 0, "max": 100, "step": 0.1, "tip": "定投增强策略的最低目标仓位。"},
            {"name": "core_max_position_pct", "label": "增强最高仓位%", "type": "number", "default": 92.0, "min": 0, "max": 100, "step": 0.1, "tip": "定投增强策略的最高目标仓位。想更激进可调高到 95~100。"},
            {"name": "strict_min_position_pct", "label": "交易最低仓位%", "type": "number", "default": 0.0, "min": 0, "max": 100, "step": 0.1, "tip": "纯交易仓模式的最低目标仓位。"},
            {"name": "strict_max_position_pct", "label": "交易最高仓位%", "type": "number", "default": 60.0, "min": 0, "max": 100, "step": 0.1, "tip": "纯交易仓模式的最高目标仓位。"},
        ],
    },
    {"type": "core_base_table", "name": "core_base_pct", "title": "趋势基础目标仓位表", "desc": "每个趋势状态下的基础目标仓位；后续再叠加估值、质量、信号和风控修正。"},
]


INPUT_SCHEMA = [
    {
        "title": "② 策略信号 / 趋势环境",
        "pill": "趋势信号",
        "tone": "neutral",
        "desc": "本区域由 strategies/families/trend_signal_control.py 的 INPUT_SCHEMA 自动生成；更换总体策略后会自动替换。",
        "fields": [
            {
                "name": "market_state",
                "label": "大趋势环境",
                "type": "choice",
                "default": "",
                "tip": "判断当前标的处在长期趋势、防守区还是震荡区。",
                "options": [
                    ["bear", "200日线下方且向下", "sell-strong", "大趋势不利，原则上不新增买入。"],
                    ["below_200", "未站上200日线", "wait", "反转确认不足，只能小仓验证。"],
                    ["above_200", "站上200日线", "buy", "可以开始做趋势仓。"],
                    ["strong_bull", "50日线 > 200日线强多头", "buy-strong", "最适合顺势持有和分批加仓。"],
                ],
            },
            {"name": "market_risk", "label": "大盘/板块同步走弱", "type": "checkbox", "default": False, "tip": "同类资产同步走弱时，单个标的信号需要降权。"},
        ],
    },
    {
        "title": "③ 入场信号",
        "pill": "趋势信号",
        "tone": "event-zone",
        "desc": "只描述当前是否具备趋势内加仓理由；场外基金低频策略不追求日内买点。",
        "fields": [
            {
                "name": "entry_state",
                "label": "入场状态",
                "type": "choice",
                "default": "",
                "tip": "没有明确买点时保持 none。",
                "options": [
                    ["reversal_50", "站回50日线反转试仓", "buy", "下跌后站回50日线，但未完全确认。"],
                    ["breakout", "平台/前高突破", "buy", "最好用收盘价确认，而不是盘中刺破。"],
                    ["pullback_hold", "回踩20/50日线不破", "buy-strong", "趋势内回踩不破通常比追高更稳。"],
                    ["continuation_high", "强趋势持续创新高", "buy-strong", "适合已有盈利后顺势持有。"],
                ],
            },
        ],
    },
    {
        "title": "④ 风险 / 结构确认",
        "pill": "趋势信号",
        "tone": "pattern-zone",
        "desc": "这些是辅助判断，只影响仓位权重，不单独决定买卖。",
        "fields": [
            {"name": "volume_confirm", "label": "突破时放量", "type": "checkbox", "default": False, "tip": "场外基金通常无真实成交量，主要给场内ETF参考。"},
            {"name": "pullback_volume_dry", "label": "回踩缩量", "type": "checkbox", "default": False, "tip": "场外基金通常无真实成交量，谨慎使用。"},
            {"name": "upper_shadow", "label": "放量长上影 / 冲高回落", "type": "checkbox", "default": False, "tip": "说明上方抛压较明显。"},
            {"name": "failed_close", "label": "收盘未站稳关键位", "type": "checkbox", "default": False, "tip": "突破确认不足。"},
            {"name": "far_from_ma", "label": "远离均线 / 涨速过快", "type": "checkbox", "default": False, "tip": "追高风险收益比下降。"},
        ],
    },
    {
        "title": "⑤ 减仓 / 清仓信号",
        "pill": "趋势信号",
        "tone": "context-zone",
        "desc": "风险信号优先于加仓信号；定投增强模式下通常先降交易仓，不直接清掉核心仓。",
        "fields": [
            {
                "name": "exit_state",
                "label": "退出状态",
                "type": "choice",
                "default": "",
                "tip": "没有破位时保持 none。",
                "options": [
                    ["below_20", "跌破20日线", "sell", "短线趋势弱化。"],
                    ["failed_breakout", "突破失败", "sell", "入场理由减弱，需要先控风险。"],
                    ["below_50", "跌破50日线", "sell-strong", "中期趋势明显受损。"],
                    ["below_200", "跌破200日线", "sell-strong", "长期趋势失守。"],
                    ["hit_stop", "触发初始止损", "sell-strong", "交易增强仓理由失效。"],
                ],
            },
        ],
    },
    {
        "title": "⑥ 盈利阶段",
        "pill": "趋势信号",
        "tone": "neutral",
        "desc": "盈利阶段只作为止盈辅助，不作为独立预测信号。",
        "fields": [
            {
                "name": "profit_state",
                "label": "盈利状态",
                "type": "choice",
                "default": "",
                "tip": "根据当前盈亏和止损距离判断。",
                "options": [
                    ["profit_1r", "盈利≥1R", "buy", "可继续持有。"],
                    ["profit_2r", "盈利≥2R", "sell", "可开始释放部分交易仓。"],
                    ["profit_3r", "盈利≥3R", "sell", "可进一步止盈。"],
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


def _cap_param(strategy: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(strategy.get(key, default)), 0.0, 100.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """趋势信号风控策略的目标仓位生成器。

    右侧解释只保留定性理由；具体仓位、百分位、修正值交给指标区显示。
    """
    strategy = get_strategy(cfg)
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    entry = str(signals.get("entry_state", "none"))
    notes: List[str] = []

    valuation_mult = _pct_param(strategy, "valuation_sensitivity_pct", 100.0)
    quality_mult = _pct_param(strategy, "quality_weight_pct", 100.0)
    risk_mult = _pct_param(strategy, "risk_penalty_pct", 100.0)
    entry_mult = _pct_param(strategy, "entry_bonus_pct", 100.0)

    base_table = strategy.get("core_base") or STRATEGY_PRESETS["balanced"]["core_base"]
    base = float(base_table.get(market, 0.50))
    target = base

    if market in {"strong_bull", "above_200"}:
        notes.append("中长期趋势偏多，核心仓可以继续在场。")
    elif market in {"bear", "below_200"}:
        notes.append("价格仍处在长期趋势防守区，新增仓位需要明显降速。")
    else:
        notes.append("震荡环境容易出现假突破，仓位以耐心等待和分批调整为主。")

    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    roe_raw = signals.get("roe_pct")
    try:
        roe = float(roe_raw) if roe_raw is not None and roe_raw != "" else None
    except (TypeError, ValueError):
        roe = None

    if pe is not None:
        if pe <= 30:
            adj = (30.0 - pe) / 30.0 * 0.10
            notes.append("估值具备一定安全边际，允许更积极地建设仓位。")
        elif pe <= 60:
            adj = (50.0 - pe) / 100.0 * 0.08
            notes.append("估值大体处在可接受区间，对仓位影响有限。")
        elif pe <= 85:
            adj = -(pe - 60.0) / 25.0 * 0.07
            notes.append("估值偏贵，新增仓位需要降速。")
        else:
            adj = -0.07 - (pe - 85.0) / 15.0 * 0.13
            notes.append("估值压力较高，追涨和一次性加仓都需要克制。")
        target += adj * valuation_mult

    if pb is not None:
        if pb <= 40:
            adj = (40.0 - pb) / 40.0 * 0.04
        elif pb <= 85:
            adj = -(pb - 40.0) / 45.0 * 0.04
        else:
            adj = -0.04 - (pb - 85.0) / 15.0 * 0.05
            notes.append("PB 也显示偏贵，进一步压低交易仓进攻性。")
        target += adj * valuation_mult

    if roe is not None:
        if roe >= 12:
            adj = min((roe - 12.0) / 13.0 * 0.06, 0.06)
            if pe is not None and pe >= 90:
                adj *= 0.45
            notes.append("盈利质量提供一定长期持有支撑。")
        else:
            adj = -min((12.0 - roe) / 12.0 * 0.07, 0.07)
            notes.append("盈利质量偏弱，不适合提高进攻性。")
        target += adj * quality_mult

    if signals.get("market_risk"):
        target -= 0.12 * risk_mult
        notes.append("大盘或同类资产同步走弱，单一标的信号需要降权。")
    if signals.get("far_from_ma"):
        target -= 0.04 * risk_mult
        notes.append("价格远离均线，追高风险上升。")
    if signals.get("upper_shadow") or signals.get("failed_close"):
        target -= 0.05 * risk_mult
        notes.append("冲高回落或收盘未确认，说明上方承接还不够稳定。")

    if exit_state == "below_20":
        target -= 0.06 * risk_mult
        notes.append("短线趋势弱化，先降低交易仓而不是直接处理核心仓。")
    elif exit_state == "failed_breakout":
        target -= 0.10 * risk_mult
        notes.append("突破失败，说明入场理由减弱，需要先控风险。")
    elif exit_state == "below_50":
        target -= 0.18 * risk_mult
        notes.append("中期趋势转弱，仓位应进入防守状态。")
    elif exit_state == "below_200":
        target -= 0.10 * risk_mult
        notes.append("长期趋势失守，优先保留防守仓位。")
    elif exit_state == "hit_stop":
        target -= 0.10 * risk_mult
        notes.append("初始止损触发，交易增强仓的理由已经失效。")

    if entry in {"pullback_hold", "breakout", "continuation_high"} and exit_state == "none":
        bonus = {"pullback_hold": 0.04, "breakout": 0.03, "continuation_high": 0.02}.get(entry, 0.0) * entry_mult
        target += bonus
        if entry == "pullback_hold":
            notes.append("回踩不破比直接追高更稳，属于较好的趋势内加仓条件。")
        elif entry == "breakout":
            notes.append("突破信号有效时可以提高交易仓，但仍要防止假突破。")
        else:
            notes.append("强趋势延续可以顺势持有，但不适合无纪律追高。")

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)

    cap = high
    if pe is not None and pe >= 95:
        cap = min(cap, _cap_param(strategy, "extreme_valuation_cap_sideways_pct", 58.0) if market in {"sideways", "below_200", "bear"} else _cap_param(strategy, "extreme_valuation_cap_trend_pct", 72.0))
    elif pe is not None and pe >= 90:
        cap = min(cap, _cap_param(strategy, "high_valuation_cap_sideways_pct", 68.0) if market in {"sideways", "below_200", "bear"} else _cap_param(strategy, "high_valuation_cap_trend_pct", 80.0))
    if market == "bear":
        cap = min(cap, _cap_param(strategy, "bear_cap_pct", 24.0))
    elif market == "below_200":
        cap = min(cap, _cap_param(strategy, "below200_cap_pct", 42.0))

    target = clamp(max(target, floor), low, cap)
    if market in {"strong_bull", "above_200"} and exit_state == "none":
        regime = "趋势多头目标"
    elif market in {"bear", "below_200"}:
        regime = "趋势防守目标"
    elif exit_state != "none":
        regime = "趋势风险降温"
    else:
        regime = "趋势震荡目标"
    signals["strategy_match_label"] = f"趋势信号风控：{regime}"
    signals["strategy_confidence"] = int(clamp(60 + abs(target - 0.50) * 45, 56, 86))
    return target, notes
