from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import (
    STRATEGY_PRESETS,
    _as_float,
    _as_optional_pct_value,
    clamp,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
    pct2,
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


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """趋势信号风控策略的目标仓位生成器。

    这部分刻意和执行层分离：
    - 这里只回答“当前状态下应该持有多少仓位”；
    - 是否交易、交易多少，仍交给最小执行变化、参数设置里的单次操作上限和回测周期处理。

    思路借鉴目标权重型组合：趋势决定基础仓位，估值/质量/风险连续修正。
    避免旧逻辑把“没有短线买点”等同于长期低仓，导致核心宽基长期跑输定投。
    """
    strategy = get_strategy(cfg)
    market = str(signals.get("market_state", "sideways"))
    exit_state = str(signals.get("exit_state", "none"))
    entry = str(signals.get("entry_state", "none"))
    notes: List[str] = []

    base_table = strategy.get("core_base") or STRATEGY_PRESETS["balanced"]["core_base"]
    base = float(base_table.get(market, 0.50))
    target = base
    notes.append(f"目标仓位模型：{strategy.get('name', '策略')}在 {market} 状态下基础仓位约 {pct2(base)}。")

    pe = _as_optional_pct_value(signals.get("pe_percentile"))
    pb = _as_optional_pct_value(signals.get("pb_percentile"))
    roe_raw = signals.get("roe_pct")
    try:
        roe = float(roe_raw) if roe_raw is not None and roe_raw != "" else None
    except (TypeError, ValueError):
        roe = None

    # PE 是主刹车，但在定投增强策略下采用连续降速，不把 70%~85% 高估直接打成低仓。
    if pe is not None:
        if pe <= 30:
            adj = (30.0 - pe) / 30.0 * 0.10
        elif pe <= 60:
            adj = (50.0 - pe) / 100.0 * 0.08
        elif pe <= 85:
            adj = -(pe - 60.0) / 25.0 * 0.07
        else:
            adj = -0.07 - (pe - 85.0) / 15.0 * 0.13
        target += adj
        notes.append(f"PE百分位 {pe:.1f}% 对目标仓位修正 {pct2(adj)}。")

    # PB 作为辅助刹车，权重低于 PE；PB 很高时压低交易仓，但不否定长期资产质量。
    if pb is not None:
        if pb <= 40:
            adj = (40.0 - pb) / 40.0 * 0.04
        elif pb <= 85:
            adj = -(pb - 40.0) / 45.0 * 0.04
        else:
            adj = -0.04 - (pb - 85.0) / 15.0 * 0.05
        target += adj
        notes.append(f"PB百分位 {pb:.1f}% 对目标仓位修正 {pct2(adj)}。")

    # 质量只微调：高 ROE 能提高“长期在场”的合理性，但不能完全抵消极端高估。
    if roe is not None:
        if roe >= 12:
            adj = min((roe - 12.0) / 13.0 * 0.06, 0.06)
            if pe is not None and pe >= 90:
                adj *= 0.45
        else:
            adj = -min((12.0 - roe) / 12.0 * 0.07, 0.07)
        target += adj
        notes.append(f"ROE {roe:.1f}% 对目标仓位修正 {pct2(adj)}。")

    # 形态/风险只影响交易仓，不把增强仓变成短线信号。
    if signals.get("market_risk"):
        target -= 0.12
        notes.append("市场同步风险出现，目标仓位下调 12%。")
    if signals.get("far_from_ma"):
        target -= 0.04
        notes.append("价格远离均线，交易仓降速 4%。")
    if signals.get("upper_shadow") or signals.get("failed_close"):
        target -= 0.05
        notes.append("冲高回落/收盘未确认，交易仓下调 5%。")

    if exit_state == "below_20":
        target -= 0.06
        notes.append("跌破20日线只降低交易仓 6%，不直接卖出核心配置仓。")
    elif exit_state == "failed_breakout":
        target -= 0.10
        notes.append("突破失败，交易仓下调 10%。")
    elif exit_state == "below_50":
        target -= 0.18
        notes.append("跌破50日线，中期风险上升，目标仓位下调 18%。")
    elif exit_state == "below_200":
        target -= 0.10
        notes.append("跌破200日线，使用200日线下方的防守目标仓位。")
    elif exit_state == "hit_stop":
        target -= 0.10
        notes.append("触发初始止损，定投增强策略仅降低交易增强仓 10%，不直接硬砍核心配置仓。")

    if entry in {"pullback_hold", "breakout", "continuation_high"} and exit_state == "none":
        bonus = {"pullback_hold": 0.04, "breakout": 0.03, "continuation_high": 0.02}.get(entry, 0.0)
        target += bonus
        notes.append(f"有效买点 {entry} 出现，交易仓小幅上调 {pct2(bonus)}。")

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)

    # 极端高估 + 弱趋势时给硬上限，避免为了跑赢定投而无脑追高。
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
    notes.append(f"最终目标仓位限制在 {pct2(target)}。")
    return target, notes



