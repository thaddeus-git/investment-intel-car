# Prompt: Complete sec-lessons translations

把这个 prompt 粘贴到新对话里，启动 6 个并行翻译 agent 补完缺失的翻译。

```markdown
我需要你帮我完成 SEC 课程（https://github.com/thaddeus-git/sec-lessons）的 6 种语言翻译。这是一个 9 课系列，教如何用 SEC 披露文件（10-K, 10-Q, 8-K, Form 4, 13F, 13D/G）做竞品分析。

我已有中文原文在 `/Users/liuming/sec-lessons/zh/`，以及各语言的部分翻译。需要你 **启动 6 个并行 agent**（每个语言一个），每个 agent 做以下工作：

## 对每个语言，三步：

### 第一步：检查已有文件
运行 `find /Users/liuming/sec-lessons/{LANG} -type f | sort`，记录哪些文件已存在。

### 第二步：只翻译缺失的文件
从 `/Users/liuming/sec-lessons/zh/` 读取对应的中文原文，翻译后写入。

### 第三步：验证
写入后运行 `find /Users/liuming/sec-lessons/{LANG} -type f | wc -l` 确认总数应为 23。

---

## 各语言已有内容（不要重译这些）

### en (English) — 已有 10 个文件
已存在：lessons/0001 ~ 0006, MISSION.md, NOTES.md, README.md, RESOURCES.md
缺失：lesson 0007, 0008, 0009, reference/sec-form-types.html, learning-records/0001~0009

### es (Español) — 已有 5 个文件
已存在：lesson 0001, MISSION.md, NOTES.md, README.md, RESOURCES.md
缺失：lesson 0002~0009, reference/sec-form-types.html, learning-records/0001~0009

### ja (日本語) — 已有 3 个文件
已存在：lesson 0001, lesson 0003, README.md
缺失：lesson 0002, 0004~0009, reference/sec-form-types.html, MISSION.md, NOTES.md, RESOURCES.md, learning-records/0001~0009

### ko (한국어) — 已有 14 个文件
已存在：lesson 0001, MISSION.md, NOTES.md, README.md, RESOURCES.md, learning-records/0001~0009
缺失：lesson 0002~0009, reference/sec-form-types.html

### de (Deutsch) — 已有 4 个文件
已存在：lesson 0001, MISSION.md, NOTES.md, README.md
缺失：lesson 0002~0009, reference/sec-form-types.html, RESOURCES.md, learning-records/0001~0009

### fr (Français) — 已有 2 个文件
已存在：lesson 0001, README.md
缺失：lesson 0002~0009, reference/sec-form-types.html, MISSION.md, NOTES.md, RESOURCES.md, learning-records/0001~0009

---

## 翻译规则（对每种语言一样）

1. **翻译所有正文内容**：段落、标题、表格内容、quiz 问题/答案、blockquote、列表、real-data boxes、takeaway boxes
2. **保留原文不变的内容**：
   - 文件名和目录结构
   - 公司名：Carvana, Carmax, AutoNation, Uxin (UXIN), AutoHome (ATHM), Vanguard, BlackRock, State Street, Baupost 等
   - SEC form 代码：10-K, 10-Q, 8-K, Form 3/4/5/144, 13F, 13D, 13G, Schedule 13D, EDGAR
   - 金融/财务术语（专业术语本地化后保留英文原名在括号里，如"Gross Margin（売上総利益率）"）
   - 字段名：`value_x1000`, `shares` 等
   - 股票代码：CVNA, KMX, AN, UXIN, ATHM
   - 货币符号：$（金融领域通用）
   - HTML/CSS 结构：所有标签、class、id、anchor 完全保留
3. **必须更新**：
   - `<html lang="zh-CN">` → 对应语言（en/es/ja/ko/de/fr）
   - `<title>` 标签 → 翻译成目标语言
4. **文件命名保持不变**（都是英文名，便于 git diff）
5. **语气**：专业、可读，受众是战略投资分析师
6. **保留所有 `<style>`、`<code>`、`<pre>` 块不变**

## 方法

1. 用 6 个并行 agent（subagent_type: general-purpose），每个对应一种语言
2. 每个 agent 先 `find` 检查已有文件
3. 逐文件读取中文原文 → 翻译 → 写到对应语言路径
4. agent 返回摘要：翻译了几个文件、跳过几个、有没有问题
5. 最后验证总数 `find /Users/liuming/sec-lessons/{LANG} -type f | wc -l` 应为 23
```
