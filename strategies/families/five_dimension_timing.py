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
        return int(raw)
    return auto_value


def _vote_reason(votes: Dict[str, int]) -> str:
    """右侧解释只给理由，不展示票数/百分比/目标仓位。"""
    positive = [name for name, value in votes.items() if value > 0 and name != "风控"]
    negative = [name for name, value in votes.items() if value < 0 and name != "风控"]
    neutral = [name for name, value in votes.items() if value == 0 and name != "风控"]
    parts: List[str] = []
    if positive:
        parts.append("正向支持来自" + "、".join(positive))
    if negative:
        parts.append("主要拖累来自" + "、".join(negative))
    if neutral and not negative:
        parts.append("其余维度暂未形成明确反向压力")
    if votes.get("风控", 0) < 0:
        parts.append("额外风控负票要求降低进攻性")
    return "；".join(parts) if parts else "多维信号暂时没有形成明确方向"


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """五维择时策略：估值+资金+技术+情绪+基本面投票交叉验证。"""
    style = get_strategy(cfg)
    notes: List[str] = []
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    entry = str(signals.get("entry_state", "none"))

    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    roe = _as_optional_pct_value(signals.get("roe_pct"))

    votes: Dict[str, int] = {}

    if pe is not None:
        votes["估值"] = _vote_score(pe, bearish=85.0, neutral=55.0, bullish=35.0)
    elif pb is not None:
        votes["估值"] = _vote_score(pb, bearish=85.0, neutral=55.0, bullish=35.0)
    else:
        votes["估值"] = 0
    votes["估值"] = _manual_vote(signals, "five_valuation_vote", votes["估值"], notes, "估值维度")

    funds = 0
    if signals.get("volume_confirm") or signals.get("pullback_volume_dry"):
        funds += 1
    if signals.get("market_risk"):
        funds -= 1
    votes["资金"] = _manual_vote(signals, "five_fund_vote", int(clamp(funds, -1, 1)), notes, "资金维度")

    tech = 0
    if market in {"above_200", "strong_bull"}:
        tech += 1
    elif market in {"below_200", "bear"}:
        tech -= 1
    if entry in {"pullback_hold", "breakout", "continuation_high"}:
        tech += 1
    if exit_state in {"below_50", "below_200", "hit_stop", "failed_breakout"}:
        tech -= 1
    votes["技术"] = _manual_vote(signals, "five_tech_vote", int(clamp(tech, -1, 1)), notes, "技术维度")

    sentiment = 0
    if signals.get("far_from_ma"):
        sentiment -= 1
    if signals.get("upper_shadow") or signals.get("failed_close"):
        sentiment -= 1
    if market == "bear":
        sentiment -= 1
    votes["情绪"] = _manual_vote(signals, "five_sentiment_vote", int(clamp(sentiment, -1, 1)), notes, "情绪维度")

    if roe is None:
        votes["基本面"] = 0
    elif roe >= 16:
        votes["基本面"] = 1
    elif roe < 8:
        votes["基本面"] = -1
    else:
        votes["基本面"] = 0
    votes["基本面"] = _manual_vote(signals, "five_fundamental_vote", votes["基本面"], notes, "基本面维度")

    risk_vote = _manual_vote(signals, "five_risk_vote", 0, notes, "风控负票")
    if risk_vote < 0:
        votes["风控"] = -1

    vote_sum = sum(votes.values())
    positive = sum(1 for v in votes.values() if v > 0)
    negative = sum(1 for v in votes.values() if v < 0)

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

    signals["strategy_match_label"] = f"五维择时：{regime}"
    signals["strategy_confidence"] = int(clamp(58 + abs(vote_sum) * 6, 55, 88))

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
    notes.append(f"五维择时：{regime}，{_vote_reason(votes)}。")
    return target, notes
