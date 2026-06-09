"""轻量因子库。先服务 mini_factor_timing，后续可在这里扩展 Alpha158/360 适配层。"""

from .mini_factors import MiniFactorResult, build_mini_factor_result

__all__ = ["MiniFactorResult", "build_mini_factor_result"]
