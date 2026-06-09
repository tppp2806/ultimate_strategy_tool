from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Any, Callable, Dict, List, Tuple

StrategyHandler = Callable[[Dict[str, Any], Dict[str, Any]], Tuple[float, List[str]]]
_DEFAULT_FAMILY_KEY = "trend_signal_control"


def _iter_family_modules() -> List[ModuleType]:
    """自动发现 strategies/families 下的总体策略模块。

    新增/移除总体策略时，只需要添加/删除 families/*.py 文件；
    不再需要手动修改 registry.py。以下文件会被忽略：
    - __init__.py
    - 以下划线开头的辅助/模板文件，例如 _template.py
    """
    from . import families

    discovered: List[ModuleType] = []
    prefix = families.__name__ + "."
    for item in pkgutil.iter_modules(families.__path__, prefix):
        module_name = item.name.rsplit(".", 1)[-1]
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(item.name)
        if _is_valid_family_module(module):
            discovered.append(module)
    discovered.sort(key=_family_sort_key)
    return discovered


def _is_valid_family_module(module: ModuleType) -> bool:
    return (
        isinstance(getattr(module, "FAMILY_KEY", None), str)
        and isinstance(getattr(module, "FAMILY_META", None), dict)
        and callable(getattr(module, "target_weight", None))
    )


def _family_sort_key(module: ModuleType) -> Tuple[int, str]:
    meta = getattr(module, "FAMILY_META", {}) or {}
    order = meta.get("order", 100)
    try:
        order_i = int(order)
    except Exception:
        order_i = 100
    return order_i, str(getattr(module, "FAMILY_KEY", module.__name__))


_REGISTERED_MODULES = tuple(_iter_family_modules())

def _family_meta_with_schema(module: ModuleType) -> Dict[str, Any]:
    meta = dict(getattr(module, "FAMILY_META", {}) or {})

    input_schema = getattr(module, "INPUT_SCHEMA", None)
    meta["input_schema"] = input_schema if isinstance(input_schema, list) else []

    # 每个总体策略可以在自己的 Python 文件中声明可编辑参数。
    # 前端只负责根据 schema 渲染，不再硬编码“均衡微调”里有哪些字段。
    style_param_schema = getattr(module, "STYLE_PARAM_SCHEMA", None)
    meta["style_param_schema"] = style_param_schema if isinstance(style_param_schema, list) else []

    style_param_presets = getattr(module, "STYLE_PARAM_PRESETS", None)
    meta["style_param_presets"] = style_param_presets if isinstance(style_param_presets, dict) else {}

    # 信号驱动模式：纯交易仓时使用信号硬规则而非目标仓位模型
    meta["signal_driven"] = bool(getattr(module, "SIGNAL_DRIVEN", False))
    return meta


STRATEGY_FAMILIES: Dict[str, Dict[str, Any]] = {
    mod.FAMILY_KEY: _family_meta_with_schema(mod) for mod in _REGISTERED_MODULES
}
STRATEGY_HANDLERS: Dict[str, StrategyHandler] = {
    mod.FAMILY_KEY: mod.target_weight for mod in _REGISTERED_MODULES
}
DEFAULT_STRATEGY_FAMILY = _DEFAULT_FAMILY_KEY if _DEFAULT_FAMILY_KEY in STRATEGY_FAMILIES else next(iter(STRATEGY_FAMILIES), _DEFAULT_FAMILY_KEY)

if not STRATEGY_FAMILIES:
    raise RuntimeError("未发现任何总体策略：请检查 strategies/families/*.py 是否提供 FAMILY_KEY / FAMILY_META / target_weight")


def normalise_strategy_family_key(key: Any) -> str:
    key = str(key or DEFAULT_STRATEGY_FAMILY)
    return key if key in STRATEGY_FAMILIES else DEFAULT_STRATEGY_FAMILY


def normalise_strategy_family_config(cfg: Dict[str, Any]) -> None:
    cfg["strategy_family"] = normalise_strategy_family_key(cfg.get("strategy_family", DEFAULT_STRATEGY_FAMILY))


def get_strategy_family(cfg: Dict[str, Any]) -> Dict[str, Any]:
    normalise_strategy_family_config(cfg)
    key = cfg["strategy_family"]
    item = dict(STRATEGY_FAMILIES[key])
    item["key"] = key
    return item


def strategy_family_summary(cfg: Dict[str, Any]) -> str:
    family = get_strategy_family(cfg)
    axes = " / ".join(family.get("axes", []))
    suffix = f"；维度：{axes}" if axes else ""
    return f"总体策略：{family['name']}（{family.get('status', '研究中')}）<br>{family.get('desc', '')}{suffix}"


def get_strategy_handler(key: Any) -> StrategyHandler:
    family_key = normalise_strategy_family_key(key)
    return STRATEGY_HANDLERS.get(family_key, STRATEGY_HANDLERS[DEFAULT_STRATEGY_FAMILY])
