from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .base import (
    ADVANCED_PARAM_DEFAULTS,
    ADVANCED_PARAM_KEYS,
    STRATEGY_MARKET_STATES,
    STRATEGY_PRESETS,
    _ADVANCED_PCT_KEYS,
    advanced_bool,
    advanced_pct,
    core_asset_floor_bounds,
    core_asset_profile,
    get_strategy,
    lower_floor,
    normalise_strategy_lab_config,
    strategy_mix_summary,
    style_mix_summary,
)
from .registry import (
    DEFAULT_STRATEGY_FAMILY,
    STRATEGY_FAMILIES,
    get_strategy_handler,
    normalise_strategy_family_config,
    normalise_strategy_family_key,
    strategy_family_summary,
)


def full_strategy_summary(cfg: Dict[str, Any]) -> str:
    return f"{strategy_family_summary(cfg)}<br>{style_mix_summary(cfg)}"


def core_target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """总体策略分发器。

    这里切换的是“全新的策略方式”，不是防守/均衡/进攻这种参数风格。
    新增总体策略时，不需要改 app.py，也不需要改 registry.py；只需要：
    1. 在 strategies/families/ 下新增一个非下划线开头的 .py；
    2. 提供 FAMILY_KEY / FAMILY_META / target_weight(cfg, signals)；
    3. 重启 Flask，顶部总体策略卡片会自动出现。
    """
    handler = get_strategy_handler(cfg.get("strategy_family"))
    return handler(cfg, signals)
