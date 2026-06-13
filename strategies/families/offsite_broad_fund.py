"""场外宽基基金低频配置策略。

适用范围：
- 场外宽基指数基金 / ETF 联接 / QDII 宽基基金。
- 低频手动交易：周频、半月频、月频更合适。

策略纯净性：
- 主要读取净值趋势、均线、估值百分位、回撤、波动、RSI/MACD/BOLL、ROE。
- 不使用成交量作为核心买卖依据；场外基金没有真实成交量，volume_confirm / pullback_volume_dry 默认不参与。
- 不要求用户手动选择宽基类型；根据 cfg.symbol_name / cfg.symbol / cfg.asset_kind 自动识别普通宽基、全球核心、成长高波动、价值红利等 profile。
- 输出的是目标仓位 0~1，不是本次买卖比例。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..base import (
    _as_optional_pct_value,
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
    pct2,
)


FAMILY_KEY = "offsite_broad_fund"
SIGNAL_DRIVEN = False

FAMILY_META: Dict[str, Any] = {
    "order": 35,
    "name": "场外宽基基金策略",
    "short_name": "场外宽基",
    "desc": "面向场外宽基基金的低频目标仓位模型：弱化量能，结合估值、净值趋势、回撤和波动控制买卖节奏。",
    "status": "可回测",
    "axes": ["场外基金", "估值", "净值趋势", "回撤", "波动"],
}


STYLE_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "risk_multiplier": 1.00,
        "trend_weight_pct": 100.0,
        "valuation_weight_pct": 100.0,
        "drawdown_weight_pct": 100.0,
        "volatility_penalty_pct": 100.0,
        "quality_weight_pct": 70.0,
        "overheat_reduce_pct": 12.0,
        "bear_cap_pct": 18.0,
        "below200_cap_pct": 36.0,
        "risk_event_cap_pct": 42.0,
        "high_valuation_cap_pct": 54.0,
        "extreme_valuation_cap_pct": 42.0,
        "core_base": {
            "bear": 0.06,
            "below_200": 0.16,
            "sideways": 0.32,
            "above_200": 0.48,
            "strong_bull": 0.58,
        },
        "trade_step_limit_enabled": True,
        "buy_step_limit_pct": 20.0,
        "sell_step_limit_pct": 32.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 92.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 62.0,
    },
    "defensive": {
        "risk_multiplier": 0.78,
        "trend_weight_pct": 85.0,
        "valuation_weight_pct": 118.0,
        "drawdown_weight_pct": 82.0,
        "volatility_penalty_pct": 125.0,
        "quality_weight_pct": 55.0,
        "overheat_reduce_pct": 16.0,
        "bear_cap_pct": 12.0,
        "below200_cap_pct": 28.0,
        "risk_event_cap_pct": 34.0,
        "high_valuation_cap_pct": 46.0,
        "extreme_valuation_cap_pct": 34.0,
        "core_base": {
            "bear": 0.03,
            "below_200": 0.10,
            "sideways": 0.24,
            "above_200": 0.38,
            "strong_bull": 0.48,
        },
        "trade_step_limit_enabled": True,
        "buy_step_limit_pct": 12.0,
        "sell_step_limit_pct": 42.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 88.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 52.0,
    },
    "aggressive": {
        "risk_multiplier": 1.18,
        "trend_weight_pct": 118.0,
        "valuation_weight_pct": 82.0,
        "drawdown_weight_pct": 116.0,
        "volatility_penalty_pct": 82.0,
        "quality_weight_pct": 90.0,
        "overheat_reduce_pct": 9.0,
        "bear_cap_pct": 25.0,
        "below200_cap_pct": 46.0,
        "risk_event_cap_pct": 52.0,
        "high_valuation_cap_pct": 66.0,
        "extreme_valuation_cap_pct": 54.0,
        "core_base": {
            "bear": 0.10,
            "below_200": 0.24,
            "sideways": 0.40,
            "above_200": 0.58,
            "strong_bull": 0.68,
        },
        "trade_step_limit_enabled": True,
        "buy_step_limit_pct": 28.0,
        "sell_step_limit_pct": 26.0,
        "core_min_position_pct": 5.0,
        "core_max_position_pct": 95.0,
        "strict_min_position_pct": 0.0,
        "strict_max_position_pct": 72.0,
    },
}


STYLE_PARAM_SCHEMA: List[Dict[str, Any]] = [
    {
        "title": "场外宽基因子权重",
        "desc": "控制估值、净值趋势、回撤、波动和质量对目标仓位的影响。成交量不作为核心因子。",
        "fields": [
            {"name": "trend_weight_pct", "label": "趋势权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "MA200/MA50状态、60/120日收益、MACD等净值趋势因子的影响强度。"},
            {"name": "valuation_weight_pct", "label": "估值权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "PE/PB百分位对仓位的影响强度；适合沪深300、上证50等有估值数据的宽基。"},
            {"name": "drawdown_weight_pct", "label": "回撤权重%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "对近一年回撤的逆向配置强度；趋势未修复时不会盲目抄底。"},
            {"name": "volatility_penalty_pct", "label": "波动惩罚%", "type": "number", "default": 100.0, "min": 0, "max": 200, "step": 1, "tip": "波动率、ATR、BOLL带宽过高时的降仓强度。"},
            {"name": "quality_weight_pct", "label": "质量权重%", "type": "number", "default": 70.0, "min": 0, "max": 200, "step": 1, "tip": "ROE质量因子的影响强度；场外宽基通常只作为辅助。"},
            {"name": "overheat_reduce_pct", "label": "过热降仓%", "type": "number", "default": 12.0, "min": 0, "max": 40, "step": 0.5, "tip": "RSI过高、BOLL上轨外、远离均线时最多压低的目标仓位。"},
        ],
    },
    {
        "title": "场外宽基仓位上限",
        "desc": "用于限制熊市、200日线下、风险事件和高估值环境下的最高仓位。",
        "fields": [
            {"name": "bear_cap_pct", "label": "熊市上限%", "type": "number", "default": 18.0, "min": 0, "max": 100, "step": 0.5, "tip": "MA200下行且价格在MA200下方时的最高目标仓位。"},
            {"name": "below200_cap_pct", "label": "200日线下上限%", "type": "number", "default": 36.0, "min": 0, "max": 100, "step": 0.5, "tip": "价格未站上MA200时的最高目标仓位。"},
            {"name": "risk_event_cap_pct", "label": "风险事件上限%", "type": "number", "default": 42.0, "min": 0, "max": 100, "step": 0.5, "tip": "市场风险、基金跟踪异常、汇率/限购/数据异常等额外风险出现时的最高仓位。"},
            {"name": "high_valuation_cap_pct", "label": "高估平滑上限%", "type": "number", "default": 54.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE百分位进入高位区后，仓位上限会连续向该值收敛，不在某个整数点断崖。"},
            {"name": "extreme_valuation_cap_pct", "label": "极高估平滑上限%", "type": "number", "default": 42.0, "min": 0, "max": 100, "step": 0.5, "tip": "PE百分位极高时，仓位上限会继续连续向该值收敛。"},
        ],
    },
]


INPUT_SCHEMA: List[Dict[str, Any]] = []


def _num(signals: Dict[str, Any], key: str) -> Optional[float]:
    raw = signals.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _pct_param(style: Dict[str, Any], key: str, default: float, upper: float = 500.0) -> float:
    try:
        return clamp(float(style.get(key, default)), 0.0, upper) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def _cap_param(style: Dict[str, Any], key: str, default: float) -> float:
    try:
        return clamp(float(style.get(key, default)), 0.0, 100.0) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def _bool_signal(signals: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = signals.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "none"}
    return bool(value)


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 1.0 if x >= edge1 else 0.0
    t = clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t, 0.0, 1.0)


def _fund_profile(cfg: Dict[str, Any]) -> str:
    """根据标的名称/代码自动识别宽基属性，不要求用户手动输入。"""
    text = f"{cfg.get('symbol_name') or ''} {cfg.get('symbol') or ''} {cfg.get('asset_kind') or ''}".lower()
    if any(key in text for key in ["纳指", "nasdaq", "qqq", "ndx", "创业", "科创", "科技", "互联网", "100etf联接"]):
        return "growth"
    if any(key in text for key in ["标普", "sp500", "s&p", "spx", "spy", "全球", "msci", "海外", "qdii"]):
        return "global_core"
    if any(key in text for key in ["红利", "价值", "上证50", "50etf", "银行", "低波", "央企"]):
        return "value"
    return "standard"


def _fund_profile_label(profile: str) -> str:
    return {
        "standard": "普通宽基",
        "global_core": "全球核心/标普500/QDII",
        "growth": "高波动成长/纳指/创业板/科创",
        "value": "价值/红利/上证50",
    }.get(profile, "普通宽基")


def _valuation_cap(high: float, style: Dict[str, Any], pe: Optional[float]) -> float:
    """PE高位上限连续收敛，避免 89/90 这种断崖。"""
    if pe is None or pe < 80.0:
        return high
    high_cap = _cap_param(style, "high_valuation_cap_pct", 54.0)
    extreme_cap = _cap_param(style, "extreme_valuation_cap_pct", 42.0)
    if pe < 90.0:
        return min(high, _lerp(high, high_cap, _smoothstep(80.0, 90.0, pe)))
    return min(high, _lerp(high_cap, extreme_cap, _smoothstep(90.0, 100.0, pe)))


def _overheat_score(signals: Dict[str, Any]) -> float:
    score = 0.0
    rsi14 = _num(signals, "rsi14")
    boll_b = _num(signals, "boll_percent_b")
    d20 = _num(signals, "distance_ma20_pct")
    d50 = _num(signals, "distance_ma50_pct")

    if rsi14 is not None:
        score = max(score, _smoothstep(66.0, 82.0, rsi14))
    if boll_b is not None:
        score = max(score, _smoothstep(88.0, 120.0, boll_b))
    if d20 is not None:
        score = max(score, _smoothstep(4.0, 12.0, d20))
    if d50 is not None:
        score = max(score, _smoothstep(8.0, 20.0, d50) * 0.75)
    if _bool_signal(signals, "far_from_ma"):
        score = max(score, 0.65)
    return clamp(score, 0.0, 1.0)


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """返回目标仓位 0~1 和解释文本。"""
    style = get_strategy(cfg)
    notes: List[str] = []

    market = str(signals.get("market_state", "sideways") or "sideways")
    exit_state = str(signals.get("exit_state", "none") or "none")
    fund_type = _fund_profile(cfg)

    trend_weight = _pct_param(style, "trend_weight_pct", 100.0)
    valuation_weight = _pct_param(style, "valuation_weight_pct", 100.0)
    drawdown_weight = _pct_param(style, "drawdown_weight_pct", 100.0)
    vol_penalty = _pct_param(style, "volatility_penalty_pct", 100.0)
    quality_weight = _pct_param(style, "quality_weight_pct", 70.0)
    overheat_reduce = _pct_param(style, "overheat_reduce_pct", 12.0, upper=100.0)

    if fund_type == "global_core":
        valuation_weight *= 0.86
        vol_penalty *= 0.92
        notes.append("自动识别为全球核心/标普500/QDII：估值和波动惩罚略放宽，但仍保留趋势纪律。")
    elif fund_type == "growth":
        vol_penalty *= 1.18
        overheat_reduce *= 1.20
        notes.append("自动识别为高波动成长宽基：提高波动和过热惩罚，避免净值大幅拉升后追高。")
    elif fund_type == "value":
        valuation_weight *= 1.12
        drawdown_weight *= 1.08
        notes.append("自动识别为价值/红利类宽基：更重视估值分位和回撤性价比。")
    else:
        notes.append("自动识别为普通宽基基金：按低频均衡配置规则执行。")

    base_table = style.get("core_base") or {
        "bear": 0.06,
        "below_200": 0.16,
        "sideways": 0.32,
        "above_200": 0.48,
        "strong_bull": 0.58,
    }
    target = float(base_table.get(market, base_table.get("sideways", 0.32)))

    if market == "strong_bull":
        notes.append("净值处于强趋势环境，基础目标仓位偏高。")
    elif market == "above_200":
        notes.append("净值在200日线上方，允许配置仓位跟随趋势。")
    elif market == "sideways":
        notes.append("净值处于震荡区，目标仓位保持中性偏低。")
    elif market == "below_200":
        notes.append("净值未站上200日线，策略以小仓观察和分批配置为主。")
    else:
        notes.append("净值处于熊市/大空头环境，策略进入防守仓位。")

    # 1) 趋势：只看净值/均线/动量，不使用场外基金无意义的成交量。
    ret60 = _num(signals, "return_60d")
    ret120 = _num(signals, "return_120d")
    ma50_slope = _num(signals, "ma50_slope_20d")
    ma200_slope = _num(signals, "ma200_slope_20d")
    macd_bar = _num(signals, "macd_bar_pct")

    trend_adj = 0.0
    if ret60 is not None:
        trend_adj += clamp(ret60 / 18.0, -0.50, 0.50) * 0.07
    if ret120 is not None:
        trend_adj += clamp(ret120 / 30.0, -0.40, 0.40) * 0.05
    if ma50_slope is not None:
        trend_adj += clamp(ma50_slope / 5.0, -0.25, 0.25) * 0.05
    if ma200_slope is not None:
        trend_adj += clamp(ma200_slope / 3.5, -0.25, 0.25) * 0.04
    if macd_bar is not None:
        trend_adj += clamp(macd_bar / 0.9, -0.25, 0.25) * 0.04
    if trend_adj:
        target += trend_adj * trend_weight
        notes.append(f"净值趋势因子对目标仓位修正 {pct2(trend_adj * trend_weight)}。")

    # 2) 估值：PE为主，PB为辅；缺失时中性处理。
    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    valuation_adj = 0.0
    if pe is not None:
        pe_score = clamp((55.0 - pe) / 45.0, -1.0, 1.0)
        valuation_adj += pe_score * (0.12 if pe_score >= 0 else 0.18)
        if pe <= 30:
            notes.append(f"PE百分位 {pe:.1f}% 偏低，估值提供正向支持。")
        elif pe >= 85:
            notes.append(f"PE百分位 {pe:.1f}% 偏高，新增仓位需要降速。")
        else:
            notes.append(f"PE百分位 {pe:.1f}% 处于中性区间。")
    if pb is not None:
        pb_score = clamp((55.0 - pb) / 45.0, -1.0, 1.0)
        valuation_adj += pb_score * (0.04 if pb_score >= 0 else 0.07)
        notes.append(f"PB百分位 {pb:.1f}% 作为辅助估值修正。")
    if pe is None and pb is None:
        notes.append("缺少PE/PB历史百分位，估值因子按中性处理。")
    else:
        target += valuation_adj * valuation_weight

    # 3) 回撤：逆向配置，但趋势未修复时不盲目抄底。
    drawdown = _num(signals, "drawdown_252d")
    if drawdown is not None:
        dd_adj = 0.0
        if drawdown >= -5.0:
            dd_adj = -0.02 if market in {"above_200", "strong_bull"} else 0.0
        elif drawdown >= -15.0:
            dd_adj = 0.04 if market in {"sideways", "above_200", "strong_bull"} else 0.01
        elif drawdown >= -30.0:
            dd_adj = 0.08 if market in {"above_200", "strong_bull"} else 0.02
        else:
            dd_adj = -0.04 if market in {"bear", "below_200"} else 0.05
        target += dd_adj * drawdown_weight
        notes.append(f"近一年回撤 {drawdown:.2f}%，按趋势修复情况修正仓位 {pct2(dd_adj * drawdown_weight)}。")

    # 4) 波动：波动升高只做降仓，不把低波动当强买点。
    vol60 = _num(signals, "volatility_60d")
    vol20 = _num(signals, "volatility_20d")
    atr_pct = _num(signals, "atr_pct")
    boll_width = _num(signals, "boll_width_pct")
    vol_adj = 0.0
    if vol60 is not None and vol60 > 24.0:
        vol_adj -= _smoothstep(24.0, 55.0, vol60) * 0.09
    if vol20 is not None and vol60 is not None and vol60 > 0:
        shock = vol20 / vol60
        if shock > 1.18:
            vol_adj -= _smoothstep(1.18, 2.10, shock) * 0.05
    if atr_pct is not None and atr_pct > 2.8:
        vol_adj -= _smoothstep(2.8, 7.0, atr_pct) * 0.05
    if boll_width is not None and boll_width > 16.0:
        vol_adj -= _smoothstep(16.0, 38.0, boll_width) * 0.04
    if vol_adj:
        target += vol_adj * vol_penalty
        notes.append(f"波动风险升高，目标仓位下调 {pct2(-vol_adj * vol_penalty)}。")

    # 5) 质量：ROE只做辅助，且高估时自动削弱。
    roe = _as_optional_pct_value(signals.get("roe_pct"))
    if roe is not None:
        quality_adj = clamp((roe - 12.0) / 18.0, -0.35, 0.45) * 0.06
        if pe is not None and pe >= 85.0 and quality_adj > 0:
            quality_adj *= 0.35
        target += quality_adj * quality_weight
        if quality_adj > 0:
            notes.append("ROE质量较好，提供轻微正向修正。")
        elif quality_adj < 0:
            notes.append("ROE偏弱，目标仓位轻微下调。")

    # 6) 过热：RSI/BOLL/远离均线只降速，不直接清仓。
    overheat = _overheat_score(signals)
    if overheat > 0:
        reduce = overheat_reduce * overheat
        target -= reduce
        notes.append(f"净值短期过热，降低追买力度 {pct2(reduce)}。")

    # 7) 风险事件 / 退出状态。
    # 不再要求用户手动勾选基金/汇率/限购等主观项；只读取系统通用 market_risk。
    extra_risk = False
    market_risk = _bool_signal(signals, "market_risk")
    if market_risk:
        target -= 0.06
        notes.append("同类资产或大盘同步走弱，策略降低风险暴露。")

    if exit_state in {"below_200", "hit_stop"}:
        target -= 0.12
        notes.append("长期趋势或止损信号触发，场外基金策略进入防守状态。")
    elif exit_state in {"below_50", "failed_breakout"}:
        target -= 0.07
        notes.append("中期结构转弱或突破失败，先降低交易增强仓。")
    elif exit_state == "below_20":
        target -= 0.03
        notes.append("跌破20日线只视为短期降温，场外基金不因短线波动大幅卖出。")

    # 风险倍率只拉伸偏离中性仓位的幅度，不直接整体乘目标，避免过度极端。
    risk_mult = clamp(float(style.get("risk_multiplier", 1.0) or 1.0), 0.1, 5.0)
    target = 0.32 + (target - 0.32) * clamp(risk_mult, 0.65, 1.35)

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)

    # 市场状态上限。
    if market == "bear":
        high = min(high, _cap_param(style, "bear_cap_pct", 18.0))
    elif market == "below_200":
        high = min(high, _cap_param(style, "below200_cap_pct", 36.0))

    # 高估上限用平滑曲线，避免硬阈值。
    high = _valuation_cap(high, style, pe)

    # 风险事件上限。
    if extra_risk or market_risk or exit_state in {"below_50", "below_200", "hit_stop", "failed_breakout"}:
        high = min(high, _cap_param(style, "risk_event_cap_pct", 42.0))

    target = clamp(max(target, floor), low, high)

    if target >= 0.52:
        regime = "积极配置"
    elif target <= 0.18:
        regime = "防守观察"
    else:
        regime = "低频均衡配置"

    # 置信度是策略一致性，不是预测胜率。缺数据时不上调太多。
    coverage = 0
    for key in ("return_60d", "return_120d", "drawdown_252d", "volatility_60d", "rsi14", "macd_bar_pct"):
        if _num(signals, key) is not None:
            coverage += 1
    if pe is not None or pb is not None:
        coverage += 1
    data_bonus = min(coverage, 7) * 2
    confidence = int(clamp(56 + abs(target - 0.32) * 55 + data_bonus, 55, 86))

    signals["strategy_match_label"] = f"场外宽基：{regime}（{_fund_profile_label(fund_type)}）"
    signals["strategy_confidence"] = confidence

    if not notes:
        notes.append("场外宽基因子没有形成明确方向，维持低频配置仓位。")
    return target, notes
