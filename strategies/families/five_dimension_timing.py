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

FAMILY_KEY = "five_dimension_timing"
FAMILY_META: Dict[str, Any] = {
    "order": 20,
    "name": "五维择时策略",
    "short_name": "五维择时",
    "desc": "估值、资金、技术、情绪、基本面五个维度投票；只有多维交叉验证时才提高仓位。当前是研究模板，资金/情绪维度先用市场风险和量价确认近似。",
    "status": "研究中",
    "axes": ["估值", "资金", "技术", "情绪", "基本面"],
}


INPUT_SCHEMA = [
    {
        "title": "② 五维投票输入",
        "pill": "五维专属",
        "tone": "event-zone",
        "desc": "这里不再使用趋势策略的买点/破位按钮，而是直接输入五个维度的投票。选“自动”时由 PE/PB、ROE 和自动行情因子估算。",
        "fields": [
            {"name": "five_valuation_vote", "label": "估值维度", "type": "select", "default": "auto", "tip": "低估给正票，高估给负票。自动模式优先使用 PE 百分位，其次 PB 百分位。", "options": [["auto", "自动：PE/PB判断"], ["1", "+1 低估/有安全边际"], ["0", "0 合理/中性"], ["-1", "-1 高估/性价比差"]]},
            {"name": "five_fund_vote", "label": "资金维度", "type": "select", "default": "auto", "tip": "观察资金是否支持上涨。没有可靠资金流数据时建议保持自动。", "options": [["auto", "自动：量能/市场风险近似"], ["1", "+1 资金确认"], ["0", "0 资金中性"], ["-1", "-1 资金走弱"]]},
            {"name": "five_tech_vote", "label": "技术维度", "type": "select", "default": "auto", "tip": "这里是技术维度的一票，不是趋势策略里的具体买点。", "options": [["auto", "自动：趋势结构判断"], ["1", "+1 技术偏多"], ["0", "0 技术中性"], ["-1", "-1 技术偏空"]]},
            {"name": "five_sentiment_vote", "label": "情绪维度", "type": "select", "default": "auto", "tip": "情绪低迷可能给正票，过热追涨通常给负票。", "options": [["auto", "自动：过热/冲高回落近似"], ["1", "+1 情绪低迷/修复"], ["0", "0 情绪中性"], ["-1", "-1 情绪过热/脆弱"]]},
            {"name": "five_fundamental_vote", "label": "基本面维度", "type": "select", "default": "auto", "tip": "目前自动模式主要用 ROE 近似。主动基金或混合基金建议谨慎手动判断。", "options": [["auto", "自动：ROE判断"], ["1", "+1 基本面较强"], ["0", "0 基本面中性"], ["-1", "-1 基本面偏弱"]]},
        ],
    },
    {
        "title": "③ 风控负票",
        "pill": "约束仓位",
        "tone": "context-zone",
        "desc": "五维择时的风控不是趋势策略的清仓信号，而是用于限制目标仓位上限。",
        "fields": [
            {"name": "five_risk_vote", "label": "风控状态", "type": "select", "default": "auto", "tip": "自动模式会根据长期趋势、市场同步风险、止损等估算。", "options": [["auto", "自动：系统风险判断"], ["0", "0 无明显风控负票"], ["-1", "-1 有明显风控负票"]]},
        ],
    },
]

def _vote_score(value: float, bearish: float, neutral: float, bullish: float) -> int:
    """把连续值转成 -1/0/+1 投票。value 越小越便宜/越好时使用。"""
    if value <= bullish:
        return 1
    if value >= bearish:
        return -1
    if value <= neutral:
        return 0
    return 0


def _manual_vote(signals: Dict[str, Any], key: str, auto_value: int, notes: List[str], label: str) -> int:
    raw = str(signals.get(key, "auto") or "auto").strip().lower()
    if raw in {"-1", "0", "1"}:
        value = int(raw)
        notes.append(f"{label}：使用手动投票 -> {value:+d}票。")
        return value
    return auto_value


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """五维择时策略：估值+资金+技术+情绪+基本面投票交叉验证。

    当前表单还没有独立的“资金/情绪”数据源，所以这里先用已有信号近似：
    - 资金：量能确认、缩量回踩、市场同步风险；
    - 情绪：远离均线、长上影、突破失败、收盘未确认；
    后续接入真实拥挤度/资金流/情绪指标时，只需要替换这两个维度的打分函数。
    """
    style = get_strategy(cfg)
    notes: List[str] = []
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    entry = str(signals.get("entry_state", "none"))

    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    roe = _as_optional_pct_value(signals.get("roe_pct"))

    votes: Dict[str, int] = {}

    # 1) 估值：越便宜越多票；极贵直接负票。
    if pe is not None:
        votes["估值"] = _vote_score(pe, bearish=85.0, neutral=55.0, bullish=35.0)
        notes.append(f"估值维度：PE百分位 {pe:.1f}% -> {votes['估值']:+d}票。")
    elif pb is not None:
        votes["估值"] = _vote_score(pb, bearish=85.0, neutral=55.0, bullish=35.0)
        notes.append(f"估值维度：PB百分位 {pb:.1f}% -> {votes['估值']:+d}票。")
    else:
        votes["估值"] = 0
        notes.append("估值维度：缺少PE/PB，按中性0票处理。")

    votes["估值"] = _manual_vote(signals, "five_valuation_vote", votes["估值"], notes, "估值维度")

    # 2) 资金：自动模式下用量价确认近似，也允许五维策略专属手动投票覆盖。
    funds = 0
    if signals.get("volume_confirm") or signals.get("pullback_volume_dry"):
        funds += 1
    if signals.get("market_risk"):
        funds -= 1
    votes["资金"] = int(clamp(funds, -1, 1))
    notes.append(f"资金维度：自动量能/市场风险合成 -> {votes['资金']:+d}票。")
    votes["资金"] = _manual_vote(signals, "five_fund_vote", votes["资金"], notes, "资金维度")

    # 3) 技术：自动模式下用趋势结构近似，但五维策略不再要求你点“买点/破位”。
    tech = 0
    if market in {"above_200", "strong_bull"}:
        tech += 1
    elif market in {"below_200", "bear"}:
        tech -= 1
    if entry in {"pullback_hold", "breakout", "continuation_high"}:
        tech += 1
    if exit_state in {"below_50", "below_200", "hit_stop", "failed_breakout"}:
        tech -= 1
    votes["技术"] = int(clamp(tech, -1, 1))
    notes.append(f"技术维度：自动趋势结构合成 -> {votes['技术']:+d}票。")
    votes["技术"] = _manual_vote(signals, "five_tech_vote", votes["技术"], notes, "技术维度")

    # 4) 情绪：自动模式下用追高和冲高回落近似，允许手动覆盖。
    sentiment = 0
    if signals.get("far_from_ma"):
        sentiment -= 1
    if signals.get("upper_shadow") or signals.get("failed_close"):
        sentiment -= 1
    if market == "bear":
        sentiment -= 1
    votes["情绪"] = int(clamp(sentiment, -1, 1))
    notes.append(f"情绪维度：自动过热/冲高回落合成 -> {votes['情绪']:+d}票。")
    votes["情绪"] = _manual_vote(signals, "five_sentiment_vote", votes["情绪"], notes, "情绪维度")

    # 5) 基本面：自动模式下用ROE近似，允许手动覆盖。
    if roe is None:
        votes["基本面"] = 0
        notes.append("基本面维度：缺少ROE，按中性0票处理。")
    elif roe >= 16:
        votes["基本面"] = 1
        notes.append(f"基本面维度：ROE {roe:.1f}% 较强 -> +1票。")
    elif roe < 8:
        votes["基本面"] = -1
        notes.append(f"基本面维度：ROE {roe:.1f}% 偏弱 -> -1票。")
    else:
        votes["基本面"] = 0
        notes.append(f"基本面维度：ROE {roe:.1f}% 中性 -> 0票。")
    votes["基本面"] = _manual_vote(signals, "five_fundamental_vote", votes["基本面"], notes, "基本面维度")

    # 风控负票只会扣分，不会增加分数。
    risk_vote = _manual_vote(signals, "five_risk_vote", 0, notes, "风控负票")
    if risk_vote < 0:
        votes["风控"] = -1

    vote_sum = sum(votes.values())
    positive = sum(1 for v in votes.values() if v > 0)
    negative = sum(1 for v in votes.values() if v < 0)

    # 五维策略强调“交叉验证”：多维同向才明显增仓，分歧大则中低仓。
    if vote_sum >= 3 and positive >= 3:
        target = 0.78
        regime = "强多维共振"
    elif vote_sum >= 2 and positive >= 2:
        target = 0.64
        regime = "偏多共振"
    elif vote_sum <= -3 and negative >= 3:
        target = 0.18
        regime = "强空多维共振"
    elif vote_sum <= -2 and negative >= 2:
        target = 0.30
        regime = "偏空共振"
    else:
        target = 0.48
        regime = "维度分歧/中性"

    signals["strategy_match_label"] = f"五维择时：{regime}（{vote_sum:+d}票）"
    signals["strategy_confidence"] = int(clamp(58 + abs(vote_sum) * 6, 55, 88))

    # 参数风格只负责执行性格：进攻风格允许更高目标，防守风格压低目标。
    risk_mult = clamp(float(style.get("risk_multiplier", 1.0)), 0.1, 5.0)
    target = 0.50 + (target - 0.50) * clamp(risk_mult, 0.65, 1.35)

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    if market == "bear":
        high = min(high, 0.32)
    elif market == "below_200":
        high = min(high, 0.50)
    if pe is not None and pe >= 90:
        high = min(high, 0.68)

    target = clamp(max(target, floor), low, high)
    notes.append(f"五维投票结果：{votes}，合计 {vote_sum:+d}，状态={regime}。")
    notes.append(f"参数风格={style.get('name', '风格')}，最终目标仓位限制在 {pct2(target)}。")
    return target, notes


