覆盖说明

本补丁修复两个 UI 问题：
1. 【参数风格】移动到左侧【仓位模式】下面；
2. 移除“全局参数风格 · 切换总体策略后仍保持当前选择……”说明文案；
3. 修复顶部【总体策略】下拉栏垂直不居中、参数按钮对齐异常的问题。

覆盖路径：
- static/app.js -> 项目根目录/static/app.js
- static/style.css -> 项目根目录/static/style.css
- templates/index.html -> 项目根目录/templates/index.html

覆盖后重启：
python app.py

检查记录：
- python3 -m py_compile app.py strategies/*.py strategies/families/*.py strategies/factors/*.py tools/check_project.py 通过
- node --check static/app.js 通过
- python3 tools/check_project.py 通过
- 前端静态结构检查通过：active-style-picker 唯一，且位于仓位模式之后；无重复 id；旧说明文案已移除。
