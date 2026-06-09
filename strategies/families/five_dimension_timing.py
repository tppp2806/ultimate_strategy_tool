"""五维择时策略已移除。

本文件保留为覆盖补丁占位，避免旧项目中残留的同名文件继续被
registry 自动发现。因为这里不再暴露 FAMILY_KEY / FAMILY_META / target_weight，
strategies.registry 会自动忽略它。
"""
from __future__ import annotations

DISABLED_REASON = "五维择时策略已移除；请使用趋势信号风控策略或小因子择时策略。"
