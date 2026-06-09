"""总体策略模板。

复制本文件为 my_strategy.py，并删除文件名前面的下划线，重启 Flask 后就会自动出现在顶部总体策略卡片中。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..base import clamp, core_asset_floor_bounds, core_asset_profile, get_strategy, lower_floor, pct2

FAMILY_KEY = "my_strategy"
FAMILY_META: Dict[str, Any] = {
    "order": 90,
    "name": "我的策略",
    "short_name": "我的策略",
    "desc": "用一句话说明这套总体策略的买卖逻辑。",
    "status": "研究中",
    "axes": ["估值", "趋势"],
}


def target_weight(cfg: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """返回目标仓位 0~1 和解释文本列表。"""
    style = get_strategy(cfg)
    notes: List[str] = []

    # 示例：先以 50% 为中性仓位，再按参数风格的风险倍率微调。
    risk_mult = float(style.get("risk_multiplier", 1.0) or 1.0)
    target = 0.50 + (risk_mult - 1.0) * 0.10

    floor = lower_floor(cfg, signals)
    low, high = core_asset_floor_bounds(core_asset_profile(cfg), cfg)
    target = clamp(max(target, floor), low, high)

    notes.append(f"模板策略：参数风格={style.get('name', '风格')}，目标仓位 {pct2(target)}。")
    return target, notes
