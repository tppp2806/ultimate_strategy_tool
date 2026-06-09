from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# 参数风格注册区
# -----------------------------------------------------------------------------
# 这里不是“总体策略”，而是同一个总体策略里的执行性格/风险风格。
# 例如：防守、均衡、进攻。它们只控制目标仓位表、买卖速度、风险倍率。
# 真正的总体策略在 STRATEGY_FAMILIES 里注册。

STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "defensive": {
        "name": "防守",
        "buy_step": 0.18,
        "sell_step": 0.55,
        "risk_multiplier": 0.75,
        "desc": "买入更慢，卖出更快；适合不想承受大回撤。",
        "research_note": "核心目标：降低回撤与误买；代价是可能长期低仓。",
        "core_base": {
            "bear": 0.10,
            "below_200": 0.20,
            "sideways": 0.42,
            "above_200": 0.58,
            "strong_bull": 0.70,
        },
    },
    "balanced": {
        "name": "均衡",
        "buy_step": 0.28,
        "sell_step": 0.45,
        "risk_multiplier": 1.00,
        "desc": "默认档；趋势、止损、仓位三者平衡。",
        "research_note": "核心目标：在长期持有和趋势风控之间折中。",
        "core_base": {
            "bear": 0.14,
            "below_200": 0.28,
            "sideways": 0.54,
            "above_200": 0.72,
            "strong_bull": 0.84,
        },
    },
    "aggressive": {
        "name": "进攻",
        "buy_step": 0.38,
        "sell_step": 0.35,
        "risk_multiplier": 1.25,
        "desc": "盈利后加仓更快，但破位清仓不打折。",
        "research_note": "核心目标：尽快吃到趋势；代价是震荡区更容易回撤。",
        "core_base": {
            "bear": 0.20,
            "below_200": 0.36,
            "sideways": 0.64,
            "above_200": 0.82,
            "strong_bull": 0.92,
        },
    },
}


# 兼容旧代码：app.py 里仍有不少地方使用 STRATEGY_PRESETS 这个名字。
# 语义上请把它理解为“参数风格/风险档位”，不是总体策略。
STRATEGY_PRESETS = STYLE_PRESETS

STRATEGY_MARKET_STATES: Dict[str, str] = {
    "bear": "熊市/大空头",
    "below_200": "200日线下",
    "sideways": "震荡",
    "above_200": "200日线上",
    "strong_bull": "强趋势",
}


# 设置页【参数设置】使用。数值单位均为百分数，保存后在后端转成 0~1 使用。
# 这些参数只负责“执行层/目标仓位层”的速度和边界，不改变估值/趋势信号本身。
ADVANCED_PARAM_DEFAULTS: Dict[str, Any] = {
    "trade_step_limit_enabled": True,
    "buy_step_defensive_pct": 18.0,
    "buy_step_balanced_pct": 28.0,
    "buy_step_aggressive_pct": 38.0,
    "sell_step_defensive_pct": 55.0,
    "sell_step_balanced_pct": 45.0,
    "sell_step_aggressive_pct": 35.0,
    "core_step_defensive_pct": 13.0,
    "core_step_balanced_pct": 22.0,
    "core_step_aggressive_pct": 30.0,
    "core_min_position_pct": 5.0,
    "core_max_position_pct": 92.0,
    "strict_min_position_pct": 0.0,
    "strict_max_position_pct": 60.0,
}
ADVANCED_PARAM_KEYS = tuple(ADVANCED_PARAM_DEFAULTS.keys())
_ADVANCED_PCT_KEYS = tuple(k for k in ADVANCED_PARAM_KEYS if k.endswith("_pct"))


def clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


def pct2(x: float) -> str:
    return f"{x * 100:.2f}%"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        v = float(value)
        # 避免 nan / inf 进入策略计算。
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except (TypeError, ValueError):
        return default


def advanced_pct(cfg: Dict[str, Any], key: str, default: float, min_value: float = 0.0, max_value: float = 100.0) -> float:
    """读取设置页百分数字段，并转换为 0~1。"""
    return clamp(_as_float(cfg.get(key), default), min_value, max_value) / 100.0


def advanced_bool(cfg: Dict[str, Any], key: str, default: bool = True) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.lower() not in {"0", "false", "off", "no", ""}
    return bool(value)


def normalise_strategy_key(key: Any) -> str:
    key = str(key or "balanced")
    return key if key in STRATEGY_PRESETS else "balanced"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no", "", "none"}
    return bool(value)


def _read_pct_value(value: Any, default_pct: float) -> float:
    """读取前端百分数字段。兼容 0.72 和 72 两种历史写法，统一返回 0~100。"""
    raw = _as_float(value, default_pct)
    if 0.0 <= raw <= 1.0 and default_pct > 1.0:
        raw *= 100.0
    return clamp(raw, 0.0, 100.0)


def _strategy_default_entry(key: str, cfg: Optional[Dict[str, Any]] = None, selected_key: str = "balanced") -> Dict[str, Any]:
    cfg = cfg or {}
    preset = STRATEGY_PRESETS[key]
    core_base = preset.get("core_base") or STRATEGY_PRESETS["balanced"]["core_base"]
    return {
        "enabled": key == selected_key,
        "weight_pct": 100.0 if key == selected_key else 0.0,
        "buy_step_pct": _read_pct_value(cfg.get(f"buy_step_{key}_pct"), float(preset.get("buy_step", 0.28)) * 100.0),
        "sell_step_pct": _read_pct_value(cfg.get(f"sell_step_{key}_pct"), float(preset.get("sell_step", 0.45)) * 100.0),
        "risk_multiplier": clamp(_as_float(preset.get("risk_multiplier"), 1.0), 0.1, 5.0),
        "core_base_pct": {state: round(float(core_base.get(state, 0.5)) * 100.0, 4) for state in STRATEGY_MARKET_STATES},
    }


def normalise_strategy_lab_config(cfg: Dict[str, Any]) -> None:
    """清洗【策略实验台】配置。

    前端保存的是便于阅读/编辑的百分数；后端统一在这里归一化，避免：
    - 权重为空或总和为0；
    - 新增策略后旧 config 缺少字段；
    - 前端误填 nan/负数/超大数导致回测异常。
    """
    selected_key = normalise_strategy_key(cfg.get("strategy", "balanced"))
    cfg["strategy"] = selected_key

    # 参数风格固定为单风格。总体策略切换由 strategy_family 负责；
    # 防守/均衡/进攻只是当前总体策略下的执行性格，不再做组合风格。
    cfg["strategy_mode"] = "single"

    raw_mix = cfg.get("strategy_mix") if isinstance(cfg.get("strategy_mix"), dict) else {}
    raw_mix_has_entries = bool(raw_mix)
    normalized: Dict[str, Dict[str, Any]] = {}

    for key in STRATEGY_PRESETS:
        # 旧配置完全没有 strategy_mix 时，默认启用当前单选策略；
        # 已经存在实验台配置时，缺失的新策略默认关闭，避免被悄悄混入组合。
        default_entry = _strategy_default_entry(key, cfg, selected_key if not raw_mix_has_entries else "")
        raw_entry = raw_mix.get(key) if isinstance(raw_mix.get(key), dict) else {}
        raw_core = raw_entry.get("core_base_pct") or raw_entry.get("core_base") or {}
        default_core = default_entry["core_base_pct"]

        normalized[key] = {
            "enabled": _as_bool(raw_entry.get("enabled"), default_entry["enabled"]),
            "weight_pct": _read_pct_value(raw_entry.get("weight_pct", raw_entry.get("weight")), default_entry["weight_pct"]),
            "buy_step_pct": _read_pct_value(raw_entry.get("buy_step_pct"), default_entry["buy_step_pct"]),
            "sell_step_pct": _read_pct_value(raw_entry.get("sell_step_pct"), default_entry["sell_step_pct"]),
            "risk_multiplier": clamp(_as_float(raw_entry.get("risk_multiplier"), default_entry["risk_multiplier"]), 0.1, 5.0),
            "core_base_pct": {
                state: _read_pct_value(raw_core.get(state), default_core[state]) if isinstance(raw_core, dict) else default_core[state]
                for state in STRATEGY_MARKET_STATES
            },
        }

    # 单风格：只有当前选中的参数风格参与执行；其他风格只保留调参值，便于切换后使用。
    for key, item in normalized.items():
        item["enabled"] = key == selected_key
        item["weight_pct"] = 100.0 if key == selected_key else 0.0

    cfg["strategy_mix"] = normalized


def _entry_to_strategy(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    preset = dict(STRATEGY_PRESETS[key])
    preset["key"] = key
    preset["buy_step"] = float(entry.get("buy_step_pct", 0.0)) / 100.0
    preset["sell_step"] = float(entry.get("sell_step_pct", 0.0)) / 100.0
    preset["risk_multiplier"] = clamp(_as_float(entry.get("risk_multiplier"), preset.get("risk_multiplier", 1.0)), 0.1, 5.0)
    preset["core_base"] = {state: float(entry.get("core_base_pct", {}).get(state, 0.0)) / 100.0 for state in STRATEGY_MARKET_STATES}
    return preset


def get_strategy_mix_entries(cfg: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any], float]]:
    """返回启用策略列表：[(key, strategy, normalized_weight_0_to_1)]。"""
    normalise_strategy_lab_config(cfg)
    mix = cfg.get("strategy_mix") or {}
    entries: List[Tuple[str, Dict[str, Any], float]] = []
    total = 0.0
    for key, raw_entry in mix.items():
        if key not in STRATEGY_PRESETS or not _as_bool(raw_entry.get("enabled"), False):
            continue
        weight = clamp(_as_float(raw_entry.get("weight_pct"), 0.0), 0.0, 100.0)
        if weight <= 0:
            continue
        entries.append((key, _entry_to_strategy(key, raw_entry), weight))
        total += weight
    if total <= 0:
        key = normalise_strategy_key(cfg.get("strategy"))
        raw_entry = mix.get(key) or _strategy_default_entry(key, cfg, key)
        return [(key, _entry_to_strategy(key, raw_entry), 1.0)]
    return [(key, strategy, weight / total) for key, strategy, weight in entries]


def get_strategy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """返回当前实际执行的参数风格。

    系统固定使用单风格：防守/均衡/进攻只表示执行性格。
    真正的总体策略切换由 cfg["strategy_family"] 分发到不同 families/*.py。
    """
    normalise_strategy_lab_config(cfg)
    key = normalise_strategy_key(cfg.get("strategy", "balanced"))
    entry = cfg.get("strategy_mix", {}).get(key) or _strategy_default_entry(key, cfg, key)
    return _entry_to_strategy(key, entry)


def style_mix_summary(cfg: Dict[str, Any]) -> str:
    strategy = get_strategy(cfg)
    return f"参数风格：{strategy.get('name', '风格')}：{strategy.get('desc', '')}"


# 兼容旧 app.py 命名。
def strategy_mix_summary(cfg: Dict[str, Any]) -> str:
    return style_mix_summary(cfg)


def core_asset_profile(cfg: Dict[str, Any]) -> str:
    """仓位模式识别。

    只要用户选择【定投增强策略】，就统一启用“定投底盘 + 目标仓位”的策略；
    不再按标普500 / 纳指100 / 沪深300 / 上证50 / 普通基金做区别对待。
    """
    return "core" if str(cfg.get("position_mode", "core_satellite")) == "core_satellite" else ""


def core_asset_floor_bounds(profile: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[float, float]:
    cfg = cfg or {}
    if profile:
        # 定投增强策略允许长期在场，但仍给熊市/破位保留降仓空间。
        return (
            advanced_pct(cfg, "core_min_position_pct", ADVANCED_PARAM_DEFAULTS["core_min_position_pct"]),
            advanced_pct(cfg, "core_max_position_pct", ADVANCED_PARAM_DEFAULTS["core_max_position_pct"]),
        )
    return (
        advanced_pct(cfg, "strict_min_position_pct", ADVANCED_PARAM_DEFAULTS["strict_min_position_pct"]),
        advanced_pct(cfg, "strict_max_position_pct", ADVANCED_PARAM_DEFAULTS["strict_max_position_pct"]),
    )


def _as_optional_pct_value(value: Any) -> Optional[float]:
    """把估值/质量输入安全转成 0~100 区间的小数；缺失时返回 None。"""
    if value is None or value == "":
        return None
    try:
        return clamp(float(value), 0.0, 100.0)
    except (TypeError, ValueError):
        return None


def lower_floor(cfg: Dict[str, Any], signals: Any) -> float:
    """系统自动计算动态防守仓位。

    - 纯交易仓：0%。
    - 定投增强策略：这里仅计算“最低防守底仓”，不是最终目标仓位。
    - 最终目标仓位由当前总体策略决定；本函数只在风险/破位状态下提供下限参考。
    - 避免最低底仓过高，把不同总体策略全部顶成同一个买入比例。
    """
    if cfg.get("position_mode") == "strict_trade":
        return 0.0

    if isinstance(signals, dict):
        market_state = str(signals.get("market_state", "sideways"))
        exit_state = str(signals.get("exit_state", "none"))
        pe = signals.get("pe_percentile")
        pb = signals.get("pb_percentile")
        roe = signals.get("roe_pct")
        market_risk = bool(signals.get("market_risk"))
    else:
        market_state = str(signals or "sideways")
        exit_state = "none"
        pe = pb = roe = None
        market_risk = False

    profile = core_asset_profile(cfg)
    if profile:
        # 这里是真正的“最低防守底仓”，不是目标仓位。
        # 旧版在强趋势/200日线上把 floor 设到 68%~78%，会导致不同总体策略、
        # 不同参数风格最后都被 max(target, floor) 顶到接近同一个仓位，
        # 看起来就像策略切换没有生效。目标仓位应由各 families/*.py 的模型决定，
        # lower_floor 只负责在风险状态下给一个最低持有/防守参考。
        base_map = {
            "bear": 0.05,
            "below_200": 0.10,
            "sideways": 0.12,
            "above_200": 0.16,
            "strong_bull": 0.20,
        }
    else:
        base_map = {
            "bear": 0.00,
            "below_200": 0.03,
            "sideways": 0.08,
            "above_200": 0.12,
            "strong_bull": 0.16,
        }
    floor = base_map.get(market_state, 0.08)

    pe_v: Optional[float] = None
    pb_v: Optional[float] = None

    # 估值是“降速器”，不是定投增强策略的清仓开关。
    if pe is not None:
        pe_v = clamp(_as_float(pe), 0.0, 100.0)
        if profile:
            floor += (50.0 - pe_v) / 100.0 * 0.06
            if pe_v >= 80:
                floor -= (pe_v - 80.0) / 20.0 * 0.12
        else:
            floor += (50.0 - pe_v) / 100.0 * 0.12
            if pe_v >= 80:
                floor -= (pe_v - 80.0) / 20.0 * 0.08
    elif pb is not None:
        pb_v = clamp(_as_float(pb), 0.0, 100.0)
        floor += (50.0 - pb_v) / 100.0 * (0.05 if profile else 0.08)
        if pb_v >= 80:
            floor -= (pb_v - 80.0) / 20.0 * (0.08 if profile else 0.05)

    # ROE只做质量微调，不允许它覆盖趋势纪律。
    if roe is not None:
        roe_v = _as_float(roe)
        floor += clamp((roe_v - 12.0) / 20.0, -0.04, 0.05)

    # 系统性风险出现时，增强仓进一步变成防守仓；定投增强策略降速，但不因单个风险标签直接清零。
    if market_risk:
        floor *= (0.68 if profile else 0.55)

    # 风险事件对防守仓位设置硬上限。定投增强策略也会防守，但不会退化成纯交易仓。
    if profile:
        hard_caps = {
            "bear": 0.16,
            "below_200": 0.26,
            "hit_stop": 0.18,
            "below_50": 0.42,
            "failed_breakout": 0.52,
            "below_20": 0.60,
        }
    else:
        hard_caps = {
            "bear": 0.05,
            "below_200": 0.10,
            "hit_stop": 0.05,
            "below_50": 0.15,
            "failed_breakout": 0.20,
            "below_20": 0.25,
        }

    if market_state == "bear":
        floor = min(floor, hard_caps["bear"])
    elif market_state == "below_200":
        floor = min(floor, hard_caps["below_200"])

    if exit_state == "hit_stop":
        floor = min(floor, hard_caps["hit_stop"])
        if not profile and (market_state in {"bear", "below_200"} or market_risk or (pe_v is not None and pe_v >= 80)):
            floor = 0.0
    elif exit_state == "below_200":
        floor = min(floor, hard_caps["below_200"])
        if not profile and (market_risk or (pe_v is not None and pe_v >= 80)):
            floor = min(floor, 0.03)
    elif exit_state == "below_50":
        floor = min(floor, hard_caps["below_50"])
    elif exit_state == "failed_breakout":
        floor = min(floor, hard_caps["failed_breakout"])
    elif exit_state == "below_20":
        floor = min(floor, hard_caps["below_20"])

    low, high = core_asset_floor_bounds(profile, cfg)
    return clamp(floor, low, high)


