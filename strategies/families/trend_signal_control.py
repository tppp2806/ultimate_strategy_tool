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


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """趋势信号风控策略的目标仓位生成器。

    右侧解释只保留定性理由；具体仓位、百分位、修正值交给指标区显示。
    """
    strategy = get_strategy(cfg)
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    entry = str(signals.get("entry_state", "none"))
    notes: List[str] = []

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
        target += adj

    if pb is not None:
        if pb <= 40:
            adj = (40.0 - pb) / 40.0 * 0.04
        elif pb <= 85:
            adj = -(pb - 40.0) / 45.0 * 0.04
        else:
            adj = -0.04 - (pb - 85.0) / 15.0 * 0.05
            notes.append("PB 也显示偏贵，进一步压低交易仓进攻性。")
        target += adj

    if roe is not None:
        if roe >= 12:
            adj = min((roe - 12.0) / 13.0 * 0.06, 0.06)
            if pe is not None and pe >= 90:
                adj *= 0.45
            notes.append("盈利质量提供一定长期持有支撑。")
        else:
            adj = -min((12.0 - roe) / 12.0 * 0.07, 0.07)
            notes.append("盈利质量偏弱，不适合提高进攻性。")
        target += adj

    if signals.get("market_risk"):
        target -= 0.12
        notes.append("大盘或同类资产同步走弱，单一标的信号需要降权。")
    if signals.get("far_from_ma"):
        target -= 0.04
        notes.append("价格远离均线，追高风险上升。")
    if signals.get("upper_shadow") or signals.get("failed_close"):
        target -= 0.05
        notes.append("冲高回落或收盘未确认，说明上方承接还不够稳定。")

    if exit_state == "below_20":
        target -= 0.06
        notes.append("短线趋势弱化，先降低交易仓而不是直接处理核心仓。")
    elif exit_state == "failed_breakout":
        target -= 0.10
        notes.append("突破失败，说明入场理由减弱，需要先控风险。")
    elif exit_state == "below_50":
        target -= 0.18
        notes.append("中期趋势转弱，仓位应进入防守状态。")
    elif exit_state == "below_200":
        target -= 0.10
        notes.append("长期趋势失守，优先保留防守仓位。")
    elif exit_state == "hit_stop":
        target -= 0.10
        notes.append("初始止损触发，交易增强仓的理由已经失效。")

    if entry in {"pullback_hold", "breakout", "continuation_high"} and exit_state == "none":
        bonus = {"pullback_hold": 0.04, "breakout": 0.03, "continuation_high": 0.02}.get(entry, 0.0)
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
        cap = min(cap, 0.58 if market in {"sideways", "below_200", "bear"} else 0.72)
    elif pe is not None and pe >= 90:
        cap = min(cap, 0.68 if market in {"sideways", "below_200", "bear"} else 0.80)
    if market == "bear":
        cap = min(cap, 0.24)
    elif market == "below_200":
        cap = min(cap, 0.42)

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
