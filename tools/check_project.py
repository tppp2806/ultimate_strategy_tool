from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY_FILES = [ROOT / "app.py", *sorted((ROOT / "strategies").glob("*.py")), *sorted((ROOT / "strategies" / "families").glob("*.py"))]


def main() -> None:
    for path in PY_FILES:
        if path.name == "_template.py":
            # 模板也应该能编译，保留检查。
            pass
        py_compile.compile(str(path), doraise=True)
        print(f"OK python: {path.relative_to(ROOT)}")

    from strategies.registry import STRATEGY_FAMILIES
    print("\n已发现总体策略：")
    for key, item in STRATEGY_FAMILIES.items():
        print(f"- {key}: {item.get('name', key)}")


if __name__ == "__main__":
    main()
