# 5 个任务窗口的 Prompt（按顺序使用）

> **使用方法**：依次开 5 个新对话，每次复制一个任务的 prompt 进去。完成后把关键产物粘回原对话，让我整合 / 决定下一步。
>
> **每个任务都从 memory 加载共同上下文**，所以即使是新对话，agent 也能立刻知道项目背景，不需要看完整对话历史。

---

## 任务 T1：建立金标准 Fixture（30-60 分钟）

**目的**：在修任何代码之前，建立一份"已知正确"的真实持仓数据作为后续所有修复的判定基准。

**复制以下 prompt 到新对话**：

```
我有一个竞品监控项目（/Users/liuming/sec），需要你完成一个独立的、原子性的任务：建立金标准 fixture。

# 必读 memory（决定你做什么）
请先读这 4 个 memory 文件：
1. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/audit-2026-06-19-critical-issues.md
2. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/ground-truth-fixture-spec.md  ← 这是你的设计规范
3. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/institution-cik-master-list.md
4. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-design.md

# 你的任务

按 ground-truth-fixture-spec.md 的规范，建立 6-8 条金标准 fixture。

## 必做的核心工作
1. 选 6-8 个机构 × 竞品 × 报告期组合（参考规范的"必须包含的两类边界"）
2. 对每条 fixture，**直接从 SEC EDGAR 原始 XML** 读出真实数值（不能从我们的 DB 取）
3. 用"隐含股价 = value/shares"做单位判断，把归一化为美元的值填进 expected_value_usd
4. 每条 fixture 必须有 source_url 指向具体的 SEC XML 文件

## 选样建议（参考但不限于）
- 大美元单位机构（必含）：Vanguard CVNA 2025-12-31、FMR CVNA 2026-03-31、State Street CVNA 2025-12-31
- 小美元单位机构（必含）：T. Rowe Price 任何持仓、LSV 任何持仓
- 跨季度同标的（验证 QoQ 一致性）：选一个机构连续两期同标的
- 至少 1 条小机构小持仓（验证不会过度归一化）

## SEC XML 抓取流程
1. submissions API: https://data.sec.gov/submissions/CIK{padded}.json
   - User-Agent: "thaddeus@example.com" 必须设置
2. 找最新 13F-HR 的 accessionNumber
3. accession URL 格式：
   https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=10
   或直接拼 archive URL
4. infotable.xml 从 -index.json 拿路径
5. XML 解析找对应 ticker 的 <infoTable>，读 <value> 和 <shrsOrPrnAmt><sshPrnamt>

## 交付物（按这个清单）
1. /Users/liuming/sec/tests/fixtures/ground_truth.yaml
   - 6-8 条 golden_holdings
   - sanity_bounds 部分
2. /Users/liuming/sec/tests/fixtures/README.md
   - 每条 fixture 的来源 URL
   - 验证方法和时间
   - 已知的"过期风险"（比如 Vanguard Q1 2026 还没出）
3. /Users/liuming/sec/tests/fixtures/verify_fixtures.py
   - 一个脚本，能从 SEC 重新 fetch 当前真实值，对比 fixture 还成不成立
   - 输出: 哪几条仍然有效，哪几条值变了/期变了
4. 写入 memory：
   /Users/liuming/.claude/projects/-Users-liuming-sec/memory/ground-truth-fixtures-built.md
   记录建了哪 N 条 fixture、各自覆盖什么场景、有效时效

# 输出要求
任务完成后，请简短汇报：
- 建了几条 fixture
- 每条覆盖什么场景（大单位 / 小单位 / 跨季度）
- 哪些 fixture 已通过 SEC 实时验证
- 是否有任何 fixture 没法建（例如 SEC 文件无法访问）

完成后我会把你的汇报粘回主对话，决定下一步。

# 重要约束
- 不要修任何业务代码（不动 src/）
- 不要碰数据库（不动 data/）
- 你的工作只在 tests/fixtures/ 目录下
- User-Agent 限流：requests 间隔 0.15s
- SEC API: User-Agent 必须包含邮箱
```

---

## 任务 T2：建 SEC Adapter 防腐层（半天）

**前置**：T1 完成（fixture 文件已就绪，作为单元测试的输入）

**复制以下 prompt 到新对话**：

```
我有一个竞品监控项目（/Users/liuming/sec），需要你完成一个独立的、原子性的任务：建立 SEC Adapter 防腐层。

# 必读 memory
1. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/audit-2026-06-19-critical-issues.md
2. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-design.md  ← 这是你的设计规范
3. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/institution-cik-master-list.md
4. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/ground-truth-fixtures-built.md  ← T1 的产物
5. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/edgartools-api-guide.md

# 你的任务

按 sec-adapter-design.md 的规范，建立 src/sec_adapter.py + 单元测试。

## 必做的工作
1. 实现 SECAdapter 类，至少包含 4 个方法：
   - verify_institution_cik(cik) -> InstitutionMeta（错就 raise）
   - fetch_13f_holdings(cik, periods) -> List[Holding]（已归一化）
   - _normalize_value(value, shares, ticker) -> int（核心）
   - sanity_check_holding(h) -> List[str]
2. 定义清晰的数据类（dataclass）：
   - InstitutionMeta（包含 cik, name, has_recent_13f, last_filing_date）
   - Holding（包含 institution_cik, institution_name, ticker, value_usd, shares, report_period, accession_number 等）
3. 单元测试 tests/test_sec_adapter.py:
   - test_verify_invalid_cik_raises（用旧 BlackRock CIK 0001364742 应该抛错）
   - test_verify_correct_cik_returns_meta（用 Vanguard 0000102909）
   - test_normalize_trowe_style（隐含价 < $1 应 ×1000）
   - test_normalize_vanguard_style（隐含价 $200-$400 应不变）
   - test_normalize_suspicious_raises（隐含价 > $1000 应 raise）
   - test_against_golden_fixtures（对每条 fixture，adapter 拉的数据应该 ±tolerance 匹配）
4. 必须包住 edgartools 的已知异常：
   - 'NoneType' object has no attribute 'find'
   - AttributeError / TypeError
   - 失败的 accession_number 必须 log warning 但继续，不抛
5. 写入 memory：
   /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-built.md
   记录已实现的方法、单元测试覆盖率、遇到的难点

## 不要做
- 不要改 institutional_tracker.py（T3 任务做）
- 不要改 cross_holding.py（T4 任务做）
- 不要重跑 13F 数据（T3 任务做）
- adapter 只负责"取 + 验 + 归一化"，不写 DB

## 关键设计点（务必看 sec-adapter-design.md）
- _normalize_value 的核心算法：implied_price = value/shares
  - < $1 → ×1000
  - $1-$1000 → 不变
  - > $1000 → raise SuspiciousValueError
- 异常处理：edgartools 抛 NoneType 时不能让整个机构数据丢失，要 log + continue
- 自定义异常类：InvalidCIKError, SuspiciousValueError, ThirteenFParseError

## ⚠️ Fixture 使用约定（T1 已建好，直接用）
- `tests/fixtures/ground_truth.yaml` 的 `expected_value_usd` 字段**已经是归一化后的美元值**
  - 例：troweprice-cvna-2026q1 的 expected_value_usd = 5,590,214,000（$5.59B）
  - 来源：SEC XML 的 <value> = 5,590,214（$1000s 单位）→ ×1000 = $5.59B
  - adapter 的 _normalize_value() 输出应该直接 == 这个字段（不需要再换算）
- 同一机构同一标的会有多个 sub-advisor 子行（Vanguard CVNA 有 6 行，FMR CVNA 有 6 行），
  必须 SUM 后再归一化。fixture 的 expected_value_usd 已经是求和后的值。
- 跑 `python tests/fixtures/verify_fixtures.py` 应该输出 7/7 VALID（建库时已验过）

# 输出要求
任务完成后，简短汇报：
- adapter 实现了哪几个方法
- 单元测试通过率（pytest 输出截图/最后几行）
- 哪几条 golden fixture 通过验证
- 修复 B6（7 家零数据）的预期效果（用 adapter 重跑会拿到数据吗？）

完成后把汇报粘回主对话。
```

---

## 任务 T3：迁移采集器并重跑数据（2-3 小时）

**前置**：T2 完成（adapter 可用）

**复制以下 prompt 到新对话**：

```
我有一个竞品监控项目（/Users/liuming/sec），需要你完成一个独立的、原子性的任务：把数据采集器迁移到新 SEC Adapter，并重跑全部 13F 数据。

# 必读 memory
1. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/audit-2026-06-19-critical-issues.md
2. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-built.md  ← T2 的产物
3. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/ground-truth-fixtures-built.md  ← T1 的产物
4. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/institution-cik-master-list.md

# 你的任务

## 1. 修复 BlackRock CIK（B5）
src/config.py 里 TOP_INSTITUTIONS / INSTITUTION_STYLES 把 BlackRock 的 CIK
从 "0001364742" 改为 "0002012383"
（institution-cik-master-list.md 已确认）

## 2. 改造 src/institutional_tracker.py
当前直接调 edgartools，改为调 SECAdapter：

```python
# 旧（institutional_tracker.py:148-...）：
c = Company(inst_cik)
f13_list = list(c.get_filings(form="13F-HR").latest(4))
for f13 in f13_list:
    tf = ThirteenF(f13)  # 这里会抛 NoneType
    for row in tf.infotable.iterrows():
        # 直接存 row.Value（没归一化）

# 新：
from sec_adapter import SECAdapter, InvalidCIKError
adapter = SECAdapter()
meta = adapter.verify_institution_cik(inst_cik)
holdings = adapter.fetch_13f_holdings(inst_cik, periods=4)
for h in holdings:
    # h.value_usd 已归一化、已 sanity check
    upsert(...)
```

## 3. DB Schema 迁移
- institutional_holdings 表加 value_usd 列（如果还没有）
- 旧的 value_x1000 列保留为兼容（标 deprecated）
- 写一个迁移脚本，对现有数据按 _normalize_value 算法补 value_usd 字段

## 4. 修复 QoQ 逻辑（B4 + B9）
src/cross_holding.py 的 compute_qoq_changes / find_initiations_liquidations：
- 当前：取全表最新 2 期做对比
- 改为：按机构各自最近 2 期对比
- 排除当期无数据的机构（Vanguard 因 Q1 2026 13F 未提交不应被判清仓）

## 5. 清空旧数据，重跑
1. DELETE FROM institutional_holdings;
2. DELETE FROM cross_holding_matrix;
3. python src/institutional_tracker.py
4. 验证：
   - SELECT 应该有 ≥30 家机构（之前只 24 家）
   - BlackRock 应该出现且有近 4 期数据
   - Vanguard 不应被判清仓
   - 所有金额应该在合理范围（单股单机构 < $20B）

## 6. 跑 fixture 验证
python tests/fixtures/verify_fixtures.py
所有 fixture 都应该通过

## 7. 写入 memory
/Users/liuming/.claude/projects/-Users-liuming-sec/memory/data-rebuild-2026-06-19.md
记录：
- 重跑后的数据量
- 哪几家机构终于有数据了（B6 解决情况）
- fixture 验证结果
- 还剩什么问题

# 输出要求
任务完成后简短汇报：
- 总持仓行数
- 31 家机构里有几家有当期数据
- BlackRock 是否有 2026-03-31 数据
- Fixture 通过率
- 任何意外的发现

# 不要做
- 不要改报告生成代码（T4 做）
- 不要改 dashboard.py（T4 做）
```

---

## 任务 T4：修报告显示层 + 重新生成报告（1 小时）

**前置**：T3 完成（DB 数据已清洗）

**复制以下 prompt 到新对话**：

```
我有一个竞品监控项目（/Users/liuming/sec），需要你完成一个独立的、原子性的任务：修复报告显示层（B2/B3 + 几个 Minor），重新生成 cross-ownership 报告。

# 必读 memory
1. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/audit-2026-06-19-critical-issues.md
2. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/data-rebuild-2026-06-19.md  ← T3 的产物
3. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-built.md

# 你的任务

## 1. 修复 _fmt_m / _fmt_delta_m（B2）
src/cross_holding.py 大约 857 行附近：
- 当前：v/1000 标 $M（假设输入是 $1000s）
- 改为：v/1_000_000 标 $M（假设输入是美元）
- 同时 cross_holding_matrix 表读 value_usd 列（不再读 value_x1000）

## 2. 验证显示量级
跑一遍报告生成，目测金额：
- Vanguard CVNA 应该是 $7B 左右（不是 $7T）
- FMR 总持仓应该是 $3-4B 左右（不是 $3T）

## 3. 修几个 Minor
- 报告 header 邮箱: thaddeus@example.com → 让用户决定（暂用占位）
- 免责声明 #1 "20 家种子机构" → "31 家种子中 N 家有数据"
- 免责声明 #5 activist 数量 → 动态读取 ACTIVIST_INSTITUTIONS 长度

## 4. 加 sanity check 卡口
src/cross_holding.py 的 generate_cross_holding_report() 开头：
```python
errors = run_sanity_checks(conn)
if errors:
    raise ReportGenerationError(f"Sanity checks failed: {errors}")
```

run_sanity_checks 至少检查：
- 任何持仓 value_usd > $20B → 报警
- 任何持仓 value_usd < $1 → 报警
- "清仓" 数量 > 5 且都来自同一机构 → 报警（Vanguard 假清仓的特征）
- 通过 fixture 验证

## 5. 重新生成报告
python -c "from cross_holding import ...; generate_cross_holding_report(...)"
保存到 /Users/liuming/sec/output/cross-ownership-report-2026Q1-final.md

## 6. 写入 memory
/Users/liuming/.claude/projects/-Users-liuming-sec/memory/report-fix-2026-06-19.md
记录修复了哪些显示问题、最终报告的关键数值、还有哪些 Minor 没修

# 输出要求
任务完成后汇报：
- 修复了哪些金额显示问题
- 报告关键数值（Top 3 holders 的金额）是否在合理范围
- Sanity check 是否全通过
- 最终报告文件路径
- 仍存在的 Minor 问题清单

# 不要做
- 不要改 adapter（T2 做）
- 不要改采集器（T3 做）
- 不要重抓 SEC 数据（T3 做）
```

---

## 任务 T5：对抗性最终审计（最后一关）

**前置**：T1-T4 全部完成

**复制以下 prompt 到新对话**：

```
我有一个竞品监控项目（/Users/liuming/sec），需要你做最后一次彻底的、独立的、对抗性的数据准确性审计。这是项目向甲方交付前的最后一道关卡。

# 项目背景
- 客户：瓜子二手车（不要在报告中提及这个名字）
- 项目：基于 SEC 13F 构建的竞品交叉持股分析（对标 IHS Markit Cross Ownership Report）
- 31 家种子机构 + 17 家竞品

# 必读 memory（了解过去 24 小时做了什么）
1. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/audit-2026-06-19-critical-issues.md  ← 上一轮发现的问题
2. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/sec-adapter-built.md
3. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/data-rebuild-2026-06-19.md
4. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/report-fix-2026-06-19.md
5. /Users/liuming/.claude/projects/-Users-liuming-sec/memory/ground-truth-fixtures-built.md

# 重要：你的角色
你是**独立审计员**，不是修复者。任务是发现问题，不是解决问题。带着挑刺心态去看——
- 上一轮审计找到 5 个 Critical + 4 个 Major + 8 个 Minor
- 这一轮的目标：确认那 9 个问题是否真的修了，并找出新引入的问题

# 你的任务（4 个维度）

## 维度 1：上轮 Critical/Major 问题是否真修了
对 audit-2026-06-19-critical-issues.md 列出的 9 个问题逐一验证：
- B1 单位混存 — 现在所有持仓 value_usd 都是美元了吗？
- B2 显示放大 1000× — 报告里 Vanguard CVNA 是 $7B 不是 $7T 吗？
- B3 排名错乱 — 排名是真实的金额排序吗？
- B4 Vanguard 假清仓 — Vanguard 不再霸占 Liquidations Top 10 了吗？
- B5 BlackRock CIK — 现在 CIK 是 0002012383 吗？BlackRock 在报告里出现且持仓合理吗？
- B6 7 家零数据 — JPMorgan/BofA/Franklin 等是否进了 DB？
- B7 周期残缺 — 各机构是否都有近 4 期数据？
- B8 Active/Passive 失真 — 5 家 Index 都有了吗？
- B9 QoQ 逻辑 — 真的按机构对齐了吗？

## 维度 2：金标准 fixture 验证
直接跑 python tests/fixtures/verify_fixtures.py
- 每条 fixture 都通过吗？
- 任何"修了 B 但破了 fixture"的情况？

## 维度 3：常识检查
- 所有持仓金额 < $20B 吗？
- 每只竞品被合理数量机构持有（5-25 家）吗？
- 总持仓金额是否符合直觉（CVNA 全机构总持仓 ≈ $20-50B 量级）？
- 有没有任何"清仓"看起来像数据时滞而非真实清仓？

## 维度 4：新引入的问题（regression）
- adapter 是否引入新 bug？
- DB schema 改动是否破坏了 dashboard？
- 报告格式是否仍然对齐 IHS？

# 输出格式（与上轮一致，方便对比）

### A. 上轮问题修复确认
| 编号 | 上轮状态 | 现在状态 | 证据 |
| B1 | Critical | ✅修复 / ⚠️部分 / ❌仍存在 | 具体数据 |
...

### B. 新发现的问题
| # | 问题 | 严重程度 | 根因 | 修复建议 |

### C. Fixture 验证结果
通过 X/Y 条；失败的列表

### D. 总体评估
- Critical 数量：
- Major 数量：
- Minor 数量：
- 当前可向甲方交付吗？理由是什么？

# 工具使用
- Bash + curl + python3 + sqlite3 直接查
- 必要时用 Agent Browser (CDP, port 9222) 做网页交叉验证
- 不要相信任何"声称"——所有结论必须有具体数据/文件路径作为证据

# 重要：不要修任何代码
你的工作是审计，不是修复。任何 bug 都列在 B 节，不要直接动手改。

请开始审计。完成后把 A/B/C/D 报告粘回主对话给我看。
```

---

## 流程总结

```
当前对话（你和我）
  ├─ 4 个 memory 文件已写好 ✓
  └─ 5 个 prompt 已生成 ✓
        ↓
      你开第 1 个新对话 → 粘 T1 prompt → 跑完粘汇报回来给我
        ↓
      我看 T1 是否 OK → 你开第 2 个新对话 → 粘 T2 prompt → ...
        ↓
      ... T3 ... T4 ...
        ↓
      最后开第 5 个新对话 → 粘 T5 prompt → 拿到最终审计报告
        ↓
      我们一起决定：交付 / 还需要再改一轮
```

## 失败时的回退策略

如果某个任务窗口失败（agent 卡住、跑出意外结果），不要重开对话——
1. 先 `cat memory/<task>-built.md` 看做到哪一步了
2. 用 SendMessage 继续这个 agent
3. 如果 agent 已死，开新对话用同样的 prompt 重做（memory 保证幂等）
