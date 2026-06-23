# 模块 F 交叉持股分析 — Bug 修复交接文档

**生成时间**：2026-06-17
**审查人员**：独立 QA（独立从零审查，未信任 `gap-analysis-ihs-markit.md` 的结论）
**项目根**：`/Users/liuming/sec`
**数据库**：`data/competitor_intel.db`

---

## 0. TL;DR

代码逻辑骨架正确，但 `data/competitor_intel.db` 中的 13F 数据严重残缺：

```
institutional_holdings 表共 3 个 report_period:
  2026-03-31: 8 家机构 / 25 条持仓    ← 最新期
  2025-12-31: 1 家机构 (Vanguard) / 4 条持仓
  2024-06-30: 1 家机构 (BlackRock) / 4 条
```

`build_cross_holding_matrix` 用 `LIMIT 2` 取最近两期（2026-03-31 vs 2025-12-31），其中 7/8 机构在 2025-12-31 不存在 → 所有 QoQ 类指标（Change / Initiations / Liquidations / Top Buyers / Top Sellers / Turnover Proxy）输出全部失真。

**根因**：`src/institutional_tracker.py:150-160` 的 13F 采集器用 `latest(2)` 拉 2 期 13F 列表，**但只用 `f13_list[0]`（最新一期）**。第 2 期被请求后丢弃，无法补齐历史 QoQ 数据。

**修复这一处即可同时解决全部 6 个 🔴 bug。**

---

## 1. Bug 清单（按优先级）

### 🔴 严重（必须修复，否则所有 QoQ 指标无意义）

| # | 位置 | 症状 | 触发机制 |
|---|---|---|---|
| BUG-1 | `cross_holding.py:146-168` | `total_change_x1000` = 完整持仓额（不是真实变动） | 缺失 prior 期 → delta = current - 0 |
| BUG-2 | `institutional_tracker.py:150-160` | 13F 采集器只取最新一期，无法补历史 | `latest(2)` 拉 2 期但只用 `[0]` |
| BUG-3 | `cross_holding.py:401-431` / `dashboard.py:817-844` | Initiations=25（应=0），Liquidations=4 | curr 25 个 (cik,ticker) - prev 4 个 = 25 |
| BUG-4 | `cross_holding.py:310-353` / `dashboard.py:732-806` | Top Buyers 全是当前持仓最多的 8 家，Top Sellers 只有 Vanguard | 全部 8 家净 delta = +持仓额 |
| BUG-5 | `cross_holding.py:477-536` | 9/9 家机构 100% 标为 "High" Turnover | churn = current/(current/2) = 200% |
| BUG-6 | `cross_holding.py:494-528` / `dashboard.py:594-624` | 上期独有机构（Vanguard）参与计算 | 循环里没排除 `prev-only` 机构 |

**数据回放验证**（基于实际数据库）：

```
Top Buyers 模拟输出:
  1. State Street       +$2,179,923.3M  ← 实际为完整持仓额
  2. Morgan Stanley     +$873,987.4M
  3. Geode Capital      +$676,611.1M
  ...

Initiations = 25  (全部 8 家 × 多个 ticker)
Liquidations = 4  (Vanguard 的 4 只竞品)

Turnover Proxy:
  9 家机构全部 churn=200.0% → "High"
```

### 🟡 中等（独立 bug，可单独修）

| # | 位置 | 症状 | 修复要点 |
|---|---|---|---|
| BUG-7 | `cross_holding.py:37-41` + 报告 §免责声明 | 阈值声明 4 档，实际 3 档；当前所有机构都落 High 档 | 修复 BUG-5 后重新校准阈值 |
| BUG-8 | `institutional_tracker.py:85-103` vs 实际 DB schema | `cross_holding_matrix` 表实际列序与代码 CREATE TABLE 不一致（`peer_avg_x1000` / `total_change_x1000` 在末尾） | SQLite 按名访问不影响功能，但需统一 |
| BUG-9 | `dashboard.py:594-624` | Dashboard 与 `cross_holding.py` 重复实现 QoQ 逻辑 | 写 `cross_holding_qoq` 表，dashboard 直接读 |
| BUG-10 | `cross_holding.py:386-396` | `value=0` 的持仓被算作"持仓"，污染 init/liq 分类 | SQL 加 `HAVING SUM(value_x1000) > 0` |
| BUG-11 | `dashboard.py:1013-1094` | Vanguard 净流出 ($343M) 计入"Passive"机构柱状图 | 过滤 `prev-only` 机构 |
| BUG-12 | `institutional_tracker.py:147-213` | 13F 采集无节流，SEC 限流风险；异常被吞 | 加 `time.sleep(0.15)` + 改 logger.error |
| BUG-13 | `institutional_tracker.py:198` | `value_x1000` 单位假设（$K）无验证 | 加 sanity check（CVNA 应在 $1B-$100B 范围） |
| BUG-14 | `dashboard.py:688-690` | Tab 2 "当前 ($K)" 显示 `1,789,747,106`，与 $K 标签组合不可读 | 改 `${x/1e6:,.1f}M` 或 `${x/1e3:,.0f}` |

### 🟢 优化

| # | 位置 | 建议 |
|---|---|---|
| BUG-15 | `dashboard.py:886-895, 1028-1036` | `compute_capital_flows_by_category` 被调 2 次（每次 1-3s），合并为 1 次 |
| BUG-16 | `cross_holding.py:140-143` | `peer_avg_x1000` 用 `.mean(axis=1)` 包含 0 值，语义不准 |
| BUG-17 | `institutional_tracker.py:66` + `cross_holding.py:183` | `INSERT OR REPLACE` 在重跑时会丢失"披露但本次未报"的记录 |
| BUG-18 | `dashboard.py:548-556` | 5 家公司 `new_positions` 简单求和，语义模糊 |
| BUG-19 | `cross_holding.py:719-994` | Markdown 报告只 9 章，IHS 是 11 页；§8-10 资本流向柱状图在 Markdown 中缺失 |

---

## 2. 推荐修复顺序

### 第 1 步：修复根因（BUG-2）

**文件**：`src/institutional_tracker.py:150-160`

**当前代码**：
```python
f13_list = list(c.get_filings(form="13F-HR").latest(2))
...
if not f13_list:
    continue
# Use the latest 13F
latest_filing = f13_list[0]
```

**建议改为**：
```python
f13_list = list(c.get_filings(form="13F-HR").latest(4))  # 拉最近 4 期
...
for f13_filing in f13_list:  # 遍历每期
    try:
        tf = ThirteenF(f13_filing)
        infotable = tf.infotable
        if infotable is None or (hasattr(infotable, 'empty') and infotable.empty):
            continue
        report_period = str(tf.report_period)[:10] if hasattr(tf, 'report_period') and tf.report_period else ""
        ...
        for _, row_data in infotable.iterrows():
            ...
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO institutional_holdings  # 改 IGNORE
                        (company_id, institution_name, institution_cik,
                         filing_date, report_period, ticker, ...)
                    VALUES (?, ?, ?, ?, ?, ?, ...)
                """, (...))
            except Exception as e:
                logger.debug("  Insert holding error: %s", e)
    except Exception as e:
        logger.warning("  ThirteenF parse error for %s (period %s): %s", inst_name, report_period, e)
        continue
```

**为什么用 `INSERT OR IGNORE`**：避免重跑时覆盖历史数据。但 13F amended filings 的 value 更新需求需要后续在 `cross_holding.py:cross_holding_qoq` 表中用最新值覆盖计算结果，而非改原始 holdings。

### 第 2 步：过滤 `value=0` 与 `prev-only` 机构（BUG-3/5/6/10/11）

**文件**：`src/cross_holding.py`

**a) `_get_pairs` 加 `HAVING`**：
```python
def _get_pairs(period):
    rows = conn.execute("""
        SELECT DISTINCT ih.institution_cik, ih.institution_name, ih.ticker,
               SUM(ih.value_x1000) AS total_value,
               COALESCE(ist.style_label, 'Unclassified') AS style_label
        FROM institutional_holdings ih
        LEFT JOIN institution_styles ist ON ih.institution_cik = ist.institution_cik
        WHERE ih.report_period = ?
        GROUP BY ih.institution_cik, ih.ticker
        HAVING SUM(ih.value_x1000) > 0
    """, (period,)).fetchall()
    return {(r[0], r[2]): (r[1], r[3], r[4]) for r in rows}
```

**b) `compute_turnover_proxy` 排除 `prev-only` 机构**：
```python
results = []
for cik in qoq["institution_cik"].unique():
    inst_data = qoq[qoq["institution_cik"] == cik]
    if (inst_data["current_value"] == 0).all():  # curr 完全无持仓
        continue  # 跳过 prev-only 机构
    ...
```

**c) `compute_qoq_changes` 排除 `prev-only`**：
```python
# 过滤：只保留 curr 也有数据的机构
merged = merged[merged["value_x1000_curr"].notna() & (merged["value_x1000_curr"] > 0)]
```

**d) `find_initiations_liquidations`**：上面 a) 步已修复，HAVING 自动过滤 value=0。

### 第 3 步：Dashboard 同步修复（BUG-9/11/14）

**文件**：`src/dashboard.py`

**a) 修 Tab 2 单位格式**：
```python
display["当前 ($M)"] = display["curr_value"].apply(lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-")
display["上期 ($M)"] = display["prev_value"].apply(lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-")
```

**b) 修 Tab 6 Init/Liq 逻辑**（与 cross_holding.py 保持一致）：
```python
# Initiation: prev 缺失或 0, curr > 0
inits = qoq_merged[
    (qoq_merged["prev_value_x1000"].isna() | (qoq_merged["prev_value_x1000"] == 0))
    & (qoq_merged["value_x1000"] > 0)
]
# 排除 prev-only 机构（curr_value == 0）
inits = inits[inits["value_x1000"].notna() & (inits["value_x1000"] > 0)]
```

**c) 把 `flows` 计算从 `_render_pie_charts` 和 `_render_capital_flows_attribution` 提到 `render_cross_holding` 入口（BUG-15）**

### 第 4 步：Schema 同步（BUG-8）

**选项 A**（推荐）：删表重建
```python
conn.execute("DROP TABLE IF EXISTS cross_holding_matrix")
# 然后让 init_institutional_tables 重建
```

**选项 B**：在 `init_institutional_tables` 加 schema migration 逻辑

### 第 5 步：13F 采集节流与单位校验（BUG-12/13）

**a) 加 sleep**：
```python
import time
for i, (inst_cik, inst_name) in enumerate(TOP_INSTITUTIONS.items()):
    if i > 0:
        time.sleep(0.15)  # ~6 req/s，留余量
    ...
```

**b) 加单位 sanity check**：
```python
# 在 _get_pairs 或 compute_qoq_changes 中：
for _, r in ih[ih['report_period']==period].iterrows():
    if r['ticker'] in ('CVNA', 'KMX', 'AN', 'UXIN', 'ATHM'):
        # 单家机构单只竞品持仓应在 100K-100M ($K) 范围
        # 即 100K = $100M, 100M = $100B
        if not (1e5 <= r['value_x1000'] <= 1e8):
            logger.warning("  Unit sanity check failed: %s %s = %s", r['institution_name'], r['ticker'], r['value_x1000'])
```

### 第 6 步：补数据库（验证修复）

**不**通过 collector 补（修 BUG-2 后再补才有意义）。在 PR 描述里写明：
> "本 PR 修复 BUG-2 ~ BUG-11 + BUG-14。下一步运行 collector 重建 2-3 期 13F 数据后再次验证输出。"

---

## 3. 验证检查清单

每修一个 bug，用以下命令验证：

```bash
# 1. 重新构建 matrix
python3 -c "
import sqlite3
from src.cross_holding import (
    build_cross_holding_matrix, compute_qoq_changes,
    rank_top_buyers_sellers, find_initiations_liquidations,
    compute_turnover_proxy
)
conn = sqlite3.connect('data/competitor_intel.db')
matrix = build_cross_holding_matrix(conn)
print('=== Top Holders (前 5) ===')
print(matrix[['institution_name','total_value_x1000','total_change_x1000','turnover_proxy']].head())
print()
qoq = compute_qoq_changes(conn)
print('=== QoQ Changes (前 10) ===')
print(qoq[['institution_name','ticker','current_value','previous_value','delta_value']].head(10))
print()
buyers = rank_top_buyers_sellers(conn, 'buyers')
sellers = rank_top_buyers_sellers(conn, 'sellers')
print('=== Top Buyers ===')
print(buyers[['institution_name','total_change']].head())
print()
print('=== Top Sellers ===')
print(sellers[['institution_name','total_change']].head())
print()
inits, liqs = find_initiations_liquidations(conn)
print(f'=== Initiations: {len(inits)}, Liquidations: {len(liqs)} ===')
print(inits.head())
turnover = compute_turnover_proxy(conn)
print()
print('=== Turnover 分布 ===')
print(turnover['churn_label'].value_counts())
conn.close()
"
```

**预期输出**（修复后）：
- `total_change_x1000` < `total_value_x1000`（真实变动小于持仓额）
- `Initiations` 与 `Liquidations` 数量合理（< 5）
- Top Buyers / Top Sellers 不再全是 8 家当前持仓机构
- Turnover `High` 比例 < 50%，不再 100%

```bash
# 2. Streamlit 看板手动验证
streamlit run src/dashboard.py
# 检查 Tab 1-7 输出是否符合预期
```

---

## 4. 不要做

1. **不要重命名列或修改 schema 列序**（除非走 BUG-8 方案 A 删表重建）——会破坏现有数据
2. **不要删除 `institution_styles` 表**——Activists Tab 依赖此表
3. **不要修改 `TURNOVER_THRESHOLDS` 数值**——修复 BUG-5 后再校准才有意义
4. **不要信任 `prd/gap-analysis-ihs-markit.md` 的结论**——独立审查时发现该文档未识别 BUG-1 ~ BUG-6 这些最严重的问题
5. **不要在 PR 中重写整个 `cross_holding.py`**——bug 是局部的，按 §2 顺序逐个修
6. **不要修改 `COMPETITORS` / `TOP_INSTITUTIONS` 列表**——bug 与配置无关

---

## 5. 关键文件定位

| 关注点 | 文件 | 行号 |
|---|---|---|
| 13F 采集（BUG-2） | `src/institutional_tracker.py` | 150-213 |
| QoQ 计算（BUG-1/4） | `src/cross_holding.py` | 146-168, 214-353 |
| Init/Liq 分类（BUG-3/10） | `src/cross_holding.py` | 360-437, 386-396 |
| Turnover 计算（BUG-5/6） | `src/cross_holding.py` | 477-536 |
| Dashboard Tab（BUG-9/11/14） | `src/dashboard.py` | 594-806, 688-690, 1013-1094 |
| DB schema（BUG-8） | `src/institutional_tracker.py` | 85-103 |
| 配置文件 | `src/config.py` | 119-203 |

---

## 6. 参考文档（仅参考，不作为真理）

- **甲方期望**： `/Users/liuming/sec/reference/RLX Q3'21 Cross Ownership Report.md`（11 页 IHS 模板）
- **已有差距分析（不可信）**：`/Users/liuming/sec/prd/gap-analysis-ihs-markit.md`
- **本次审查结果**（即本文件）

---

**预计工时**：第 1-3 步约 4-6 小时（含验证），第 4-5 步约 2 小时。
