# -*- coding: utf-8 -*-
"""
策略分离修复器

用途：
- 修复【小因子择时策略】/【简易均线策略】在【纯交易仓】下被 app.py 回落到趋势信号硬规则的问题。
- 让非 SIGNAL_DRIVEN 的总体策略在纯交易仓下也走自己的 core_target_weight()/target_weight()。
- 清理模型型策略结果区里混入的趋势策略专属指标。

运行：
    python tools/fix_strategy_isolation.py

回滚：
    还原 _patch_backup/strategy_isolation_v3/ 下最近一次备份的文件即可。
"""
from __future__ import annotations

import ast
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


PATCH_ID = "strategy_isolation_v3"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "app.py").exists():
            return p
    raise SystemExit("未找到 app.py。请在仓库根目录运行。")


ROOT = repo_root()
APP = ROOT / "app.py"
MINI = ROOT / "strategies" / "families" / "mini_factor_timing.py"
BACKUP_DIR = ROOT / "_patch_backup" / PATCH_ID / datetime.now().strftime("%Y%m%d_%H%M%S")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")


def backup(path: Path) -> None:
    if not path.exists():
        return
    dst = BACKUP_DIR / path.relative_to(ROOT)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)


def func_region(text: str, name: str) -> Optional[Tuple[int, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            end = getattr(node, "end_lineno", None)
            if end is None:
                return None
            return offsets[node.lineno - 1], offsets[end]
    return None


def patch_app_model_branch(text: str) -> tuple[str, bool]:
    if f"{PATCH_ID}_model_branch" in text:
        return text, False

    # 目标：把 use_target_model = is_core_mode 改成：
    # use_target_model = is_core_mode or (not signal_driven)
    region = func_region(text, "raw_target_by_signal")
    start, end = region if region else (0, len(text))
    chunk = text[start:end]

    pattern = re.compile(r"^(?P<indent>[ \t]*)use_target_model\s*=\s*is_core_mode\s*$", re.M)
    m = pattern.search(chunk)

    if not m:
        # 兼容变量名仍在，但右侧有细微变化的本地版本
        pattern = re.compile(r"^(?P<indent>[ \t]*)use_target_model\s*=\s*(?P<rhs>.*is_core_mode.*)$", re.M)
        m = pattern.search(chunk)

    if not m:
        raise RuntimeError(
            "未找到 `use_target_model = is_core_mode`。"
            "请把 app.py 中 raw_target_by_signal 函数附近代码发我，我给你做精确 diff。"
        )

    indent = m.group("indent")
    replacement = (
        f"{indent}# {PATCH_ID}_model_branch\n"
        f"{indent}# 非信号驱动总体策略，例如 simple_ma / mini_factor_timing，\n"
        f"{indent}# 在纯交易仓下也必须使用自己的 target_weight()，不能回落到趋势信号硬规则。\n"
        f"{indent}use_target_model = is_core_mode or (not signal_driven)\n"
        f"{indent}if use_target_model:\n"
        f"{indent}    signals[\"core_target_model\"] = True\n"
        f"{indent}    signals[\"strategy_model_driven\"] = not signal_driven"
    )

    new_chunk = chunk[:m.start()] + replacement + chunk[m.end():]
    return text[:start] + new_chunk + text[end:], True


def matching_square(text: str, open_pos: int) -> int:
    depth = 0
    quote = ""
    escape = False
    for i in range(open_pos, len(text)):
        ch = text[i]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
    raise RuntimeError("未找到 metrics 列表结束位置。")


def patch_metrics_filter(text: str) -> tuple[str, bool]:
    if f"{PATCH_ID}_metrics_filter" in text:
        return text, False

    region = func_region(text, "decision_to_payload")
    start, end = region if region else (0, len(text))
    chunk = text[start:end]

    m = re.search(r"^(?P<indent>[ \t]*)metrics\s*=\s*\[", chunk, re.M)
    if not m:
        print("提示：未找到 decision_to_payload 里的 metrics = [，跳过字段过滤。")
        return text, False

    indent = m.group("indent")
    abs_open = start + chunk.find("[", m.start())
    abs_close = matching_square(text, abs_open)

    block = (
        "\n\n"
        f"{indent}# {PATCH_ID}_metrics_filter\n"
        f"{indent}# 模型型总体策略不显示趋势信号风控策略专属指标。\n"
        f"{indent}family_key_for_metrics = str(cfg.get(\"strategy_family\") or \"\")\n"
        f"{indent}if family_key_for_metrics in {{\"simple_ma\", \"mini_factor_timing\"}}:\n"
        f"{indent}    trend_only_metric_labels = {{\n"
        f"{indent}        \"预期赔率\", \"操作频率\", \"止损距离\", \"风险仓位上限\",\n"
        f"{indent}        \"本次买入上限\", \"本次卖出上限\", \"估值修正\", \"ROE修正\", \"系统防守仓位\",\n"
        f"{indent}    }}\n"
        f"{indent}    metrics = [item for item in metrics if item.get(\"label\") not in trend_only_metric_labels]\n"
    )

    return text[:abs_close + 1] + block + text[abs_close + 1:], True


def patch_mini_factor(text: str) -> tuple[str, bool]:
    if "SIGNAL_DRIVEN" in text:
        return text, False
    new = re.sub(
        r'^(FAMILY_KEY\s*=\s*["\']mini_factor_timing["\']\s*)$',
        r'\1\nSIGNAL_DRIVEN = False',
        text,
        count=1,
        flags=re.M,
    )
    if new == text:
        raise RuntimeError("mini_factor_timing.py 中未找到 FAMILY_KEY。")
    return new, True


def main() -> None:
    print("仓库：", ROOT)
    backup(APP)
    if MINI.exists():
        backup(MINI)

    changed = []

    app_text = read(APP)
    app_text, did = patch_app_model_branch(app_text)
    if did:
        changed.append("app.py：非信号驱动策略在纯交易仓下走 target_weight()")
    app_text, did = patch_metrics_filter(app_text)
    if did:
        changed.append("app.py：过滤模型型策略的趋势专属展示字段")
    write(APP, app_text)

    if MINI.exists():
        mini_text = read(MINI)
        mini_text, did = patch_mini_factor(mini_text)
        if did:
            changed.append("mini_factor_timing.py：新增 SIGNAL_DRIVEN = False")
            write(MINI, mini_text)

    ast.parse(read(APP))
    if MINI.exists():
        ast.parse(read(MINI))

    if changed:
        print("已修改：")
        for item in changed:
            print(" -", item)
        print("备份：", BACKUP_DIR)
    else:
        print("没有需要修改的内容，可能已经应用过。")


if __name__ == "__main__":
    main()
