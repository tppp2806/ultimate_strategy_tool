覆盖补丁：偏离预览简化格式

覆盖路径：
- app.py
- static/app.js
- static/style.css
- strategies/base.py

本次修复：
- 偏离预览改为极简格式，例如：卖出节奏% 48→0
- 移除“均衡基准/执行值/差值”等复杂说明
- 保留只保存【均衡】基准 + 防守/进攻偏离值的逻辑

检查：
- node --check static/app.js
- python -m py_compile app.py strategies/base.py
