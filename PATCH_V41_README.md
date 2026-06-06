# v41 覆盖文件补丁：回测表格预览窗口

覆盖到原项目根目录即可。

## 覆盖文件

```text
templates/index.html
static/app.js
static/style.css
PATCH_V41_README.md
```

## 改动

1. 【核心指标】表格固定按三列显示：

```text
指标 / 数值 / 备注
```

2. 在【核心指标】和【交易记录】栏目标题右侧新增【预览】按钮。

3. 点击【预览】会打开弹窗窗口查看完整表格，不影响页面内原有折叠表格。

4. 支持点击遮罩/关闭按钮关闭，也支持按 `Esc` 关闭。

5. 交易记录预览仍使用交易记录本身返回的列；核心指标预览强制使用 `指标 / 数值 / 备注` 三列。

## 测试

已做基础检查：

```text
node --check static/app.js
python -m py_compile app.py
```
