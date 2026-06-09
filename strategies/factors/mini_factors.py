from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..base import _as_float, _as_optional_pct_value, clamp, pct2


@dataclass
class MiniFactorResult:
    """小因子策略的可解释输出。adjustments 的单位是仓位比例，例如 0.10 = +10%。"""

    base: float = 0.50
    adjustments: Dict[str, float] = field(default_factory=dict)
    scores: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def total_adjustment(self) -> float:
        return sum(float(v or 0.0) for v in self.adjustments.values())

    @property
    def raw_target(self) -> float:
        return self.base + self.total_adjustment


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


def _score_to_adj(score: float, max_down: float, max_up: float) -> float:
    """score 为 -1~+1，映射到不对称仓位修正区间。"""
    score = clamp(score, -1.0, 1.0)
    return score * (max_up if score >= 0 else max_down)


def _trend_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    market = str(signals.get("market_state", "sideways"))
    ret60 = _num(signals, "return_60d")
    ret120 = _num(signals, "return_120d")
    ma50_slope = _num(signals, "ma50_slope_20d")
    ma200_slope = _num(signals, "ma200_slope_20d")
    macd_bar_pct = _num(signals, "macd_bar_pct")
    macd_dif_pct = _num(signals, "macd_dif_pct")
    macd_dea_pct = _num(signals, "macd_dea_pct")
    rsi14 = _num(signals, "rsi14")
    boll_percent_b = _num(signals, "boll_percent_b")

    score = {
        "bear": -0.85,
        "below_200": -0.55,
        "sideways": 0.0,
        "above_200": 0.45,
        "strong_bull": 0.75,
    }.get(market, 0.0)
    notes = [f"趋势因子：市场状态={market}，基础分 {score:+.2f}。"]

    momentum_parts = []
    for label, value, scale in [("60日动量", ret60, 18.0), ("120日动量", ret120, 28.0)]:
        if value is not None:
            part = clamp(value / scale, -0.35, 0.35)
            momentum_parts.append(part)
            notes.append(f"趋势因子：{label} {value:.2f}% -> {part:+.2f}。")
    if momentum_parts:
        score += sum(momentum_parts) / len(momentum_parts)

    slope_parts = []
    for label, value, scale in [("MA50斜率", ma50_slope, 4.0), ("MA200斜率", ma200_slope, 3.0)]:
        if value is not None:
            part = clamp(value / scale, -0.20, 0.20)
            slope_parts.append(part)
            notes.append(f"趋势因子：{label} {value:.2f}% -> {part:+.2f}。")
    if slope_parts:
        score += sum(slope_parts) / len(slope_parts)

    tech_parts = []
    if macd_bar_pct is not None:
        part = clamp(macd_bar_pct / 0.90, -0.25, 0.25)
        if macd_dif_pct is not None and macd_dea_pct is not None:
            cross_note = "DIF在DEA上方" if macd_dif_pct >= macd_dea_pct else "DIF在DEA下方"
            notes.append(f"趋势因子：MACD柱 {macd_bar_pct:.3f}%（{cross_note}）-> {part:+.2f}。")
        else:
            notes.append(f"趋势因子：MACD柱 {macd_bar_pct:.3f}% -> {part:+.2f}。")
        tech_parts.append(part)
    if rsi14 is not None:
        if rsi14 >= 75:
            part = -0.18
        elif rsi14 >= 55:
            part = 0.12
        elif rsi14 >= 45:
            part = 0.02
        elif rsi14 >= 30:
            part = -0.08
        else:
            part = -0.15
        notes.append(f"趋势因子：RSI14 {rsi14:.2f} -> {part:+.2f}。")
        tech_parts.append(part)
    if boll_percent_b is not None:
        if boll_percent_b >= 110:
            part = -0.14
        elif boll_percent_b >= 60:
            part = 0.10
        elif boll_percent_b <= -10:
            part = -0.14
        elif boll_percent_b <= 20:
            part = -0.06
        else:
            part = 0.0
        notes.append(f"趋势因子：BOLL %B {boll_percent_b:.2f} -> {part:+.2f}。")
        tech_parts.append(part)
    if tech_parts:
        score += sum(tech_parts) / len(tech_parts)

    return clamp(score, -1.0, 1.0), notes


def _valuation_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    values = []
    notes: List[str] = []
    if pe is not None:
        # PE百分位越低越好：20%约 +1，90%约 -1。
        score = clamp((55.0 - pe) / 35.0, -1.0, 1.0)
        values.append(score)
        notes.append(f"估值因子：PE百分位 {pe:.1f}% -> {score:+.2f}。")
    if pb is not None:
        score = clamp((55.0 - pb) / 38.0, -1.0, 1.0)
        values.append(score * 0.75)
        notes.append(f"估值因子：PB百分位 {pb:.1f}% -> {score * 0.75:+.2f}。")
    if not values:
        return 0.0, ["估值因子：缺少PE/PB百分位，按中性处理。"]
    return clamp(sum(values) / len(values), -1.0, 1.0), notes


def _drawdown_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    dd = _num(signals, "drawdown_252d")
    market = str(signals.get("market_state", "sideways"))
    if dd is None:
        return 0.0, ["回撤因子：缺少近一年高点回撤，按中性处理。"]

    # 轻度回撤仍在强趋势中：偏向等回踩；深度回撤但趋势未修复：不盲目抄底。
    if dd >= -5:
        score = -0.20 if market in {"strong_bull", "above_200"} else 0.0
    elif dd >= -15:
        score = 0.35 if market in {"above_200", "strong_bull", "sideways"} else -0.10
    elif dd >= -30:
        score = 0.55 if market in {"above_200", "strong_bull"} else 0.10
    else:
        score = -0.25 if market in {"bear", "below_200"} else 0.35
    return clamp(score, -1.0, 1.0), [f"回撤因子：近一年回撤 {dd:.2f}%，市场={market} -> {score:+.2f}。"]


def _volatility_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    vol20 = _num(signals, "volatility_20d")
    vol60 = _num(signals, "volatility_60d")
    atr_pct = _num(signals, "atr_pct")
    boll_width_pct = _num(signals, "boll_width_pct")
    values = []
    notes: List[str] = []
    if vol60 is not None:
        # 低波动不一定好，但对基金低频仓位系统更适合提高可承受仓位；高波动降仓。
        score = clamp((28.0 - vol60) / 24.0, -1.0, 0.6)
        values.append(score)
        notes.append(f"波动因子：60日年化波动 {vol60:.2f}% -> {score:+.2f}。")
    if vol20 is not None and vol60 is not None and vol60 > 0:
        shock = vol20 / vol60
        score = -clamp((shock - 1.15) / 0.85, 0.0, 0.7)
        values.append(score)
        notes.append(f"波动因子：20/60波动比 {shock:.2f} -> {score:+.2f}。")
    if atr_pct is not None:
        score = -clamp((atr_pct - 2.2) / 4.0, 0.0, 0.5)
        values.append(score)
        notes.append(f"波动因子：ATR占比 {atr_pct:.2f}% -> {score:+.2f}。")
    if boll_width_pct is not None:
        # BOLL宽度越大，说明净值波动区间越宽；低频基金仓位系统对高波动略降权。
        score = -clamp((boll_width_pct - 12.0) / 18.0, 0.0, 0.45)
        values.append(score)
        notes.append(f"波动因子：BOLL带宽 {boll_width_pct:.2f}% -> {score:+.2f}。")
    if not values:
        return 0.0, ["波动因子：缺少波动/ATR数据，按中性处理。"]
    return clamp(sum(values) / len(values), -1.0, 1.0), notes


def _volume_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    vr = _num(signals, "volume_ratio_20d")
    score = 0.0
    notes: List[str] = []
    if signals.get("volume_confirm"):
        score += 0.55
        notes.append("量能因子：突破/延续时放量确认 -> +0.55。")
    if signals.get("pullback_volume_dry"):
        score += 0.35
        notes.append("量能因子：回踩缩量 -> +0.35。")
    if signals.get("upper_shadow") or signals.get("failed_close"):
        score -= 0.45
        notes.append("量能因子：冲高回落/收盘未确认 -> -0.45。")
    if vr is not None and not notes:
        # 不把单纯放量当买点，只有异常放量且无确认时略谨慎。
        score += -0.15 if vr >= 1.8 else (0.10 if 0.75 <= vr <= 1.25 else 0.0)
        notes.append(f"量能因子：量比20日均量 {vr:.2f} -> {score:+.2f}。")
    if not notes:
        notes.append("量能因子：缺少成交量或基金净值无真实量能，按中性处理。")
    return clamp(score, -1.0, 1.0), notes


def _quality_factor(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    roe = _as_optional_pct_value(signals.get("roe_pct"))
    if roe is None:
        return 0.0, ["质量因子：缺少ROE，按中性处理。"]
    if roe >= 18:
        score = 0.85
    elif roe >= 12:
        score = 0.35 + (roe - 12.0) / 6.0 * 0.35
    elif roe >= 8:
        score = 0.0
    else:
        score = -clamp((8.0 - roe) / 8.0, 0.0, 1.0)
    return clamp(score, -1.0, 1.0), [f"质量因子：ROE {roe:.1f}% -> {score:+.2f}。"]


def build_mini_factor_result(cfg: Dict[str, Any], signals: Dict[str, Any]) -> MiniFactorResult:
    """构建小因子目标仓位。

    设计原则：
    - 不是把几十个技术指标堆成黑箱，而是只保留低频基金可解释的 6 类因子；
    - 每类因子输出 -1~+1 的标准分，再映射为仓位修正；
    - 单个因子不能主宰全部仓位，最后仍交给总体策略/参数风格做边界约束。
    """
    result = MiniFactorResult(base=0.50)
    factor_defs = [
        ("趋势", _trend_factor, 0.20, 0.20),
        ("估值", _valuation_factor, 0.15, 0.15),
        ("回撤", _drawdown_factor, 0.10, 0.15),
        ("波动", _volatility_factor, 0.10, 0.05),
        ("量能", _volume_factor, 0.05, 0.05),
        ("质量", _quality_factor, 0.05, 0.10),
    ]

    for name, fn, max_down, max_up in factor_defs:
        score, notes = fn(signals)
        adj = _score_to_adj(score, max_down=max_down, max_up=max_up)
        result.scores[name] = score
        result.adjustments[name] = adj
        result.notes.extend(notes)
        result.notes.append(f"{name}因子仓位修正：{score:+.2f} -> {pct2(adj)}。")

    return result
