# v44 覆盖文件补丁：修复“系统自算历史 PE 百分位”实时页面不出值

## 覆盖文件

```text
app.py
PATCH_V44_README.md
```

## 修复原因

之前实时操作页选择【系统自算历史百分位】时，后端会先走 `valuation:akshare_index` 当前估值链路。
这类接口有时只返回 1 行当前估值，因此只能解析出 `current_pe/current_pb`，无法计算历史 PE/PB 百分位。

你看到的日志：

```text
valuation:akshare_index 第valuation-1次：成功（1条）
```

并不代表已经算出 PE 百分位，只代表拿到了 1 行估值数据。

## 修复内容

实时估值链路改成：

```text
系统自算历史百分位：
1. 优先拉取历史估值序列
2. 用历史序列自行计算 PE/PB 百分位
3. 取最新一日填入操作页
4. 如果历史序列失败，再退回当前估值字段
```

新增链路标签：

```text
valuation:akshare_index_history_latest
valuation:akshare_lg_history_latest
```

## 预期效果

沪深300这类指数如果 AKShare 历史估值接口可用，操作页应能自动填入：

```text
当前PE
PE百分位
当前PB
PB百分位
估值来源：akshare_index_history_latest:...
估值提示：系统自算历史百分位
```

## 注意

如果 AKShare 对应历史估值接口只返回极少行，或接口本身没有 PE 历史列，仍然无法自算百分位。此时可以在设置页切换为【蛋卷优先】。
