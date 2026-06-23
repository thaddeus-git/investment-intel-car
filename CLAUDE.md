# CLAUDE.md — 竞品情报监控系统

> Claude Code 项目上下文文件。进入此目录时自动加载。

## 项目身份

自动化监控 5 家美股二手车赛道竞品（CVNA/KMX/AN/UXIN/ATHM）的 SEC 动向，生成中文 Streamlit 看板。将分析师手工 4-6h/次的季度竞品分析压缩到 30min 内。

**主 PRD**: `prd/PRD.md`

## 技术栈

| 层 | 选型 |
|---|------|
| SEC 数据底座 | `edgartools` (MIT, 4.6.3) |
| 8-K LLM 提取 | `edgar-parser[llm]` |
| LLM | DeepSeek v4 Flash (摘要) / Pro (纪要) |
| 看板 | Streamlit + Plotly |
| 存储 | SQLite (`data/competitor_intel.db`) |
| 定时 | `schedule` 库 |

## 项目结构

```
sec/
├── prd/PRD.md              # 主 PRD（活文档）
├── prd/reference/          # gap analysis + IHS 参考报告
├── prd/archive/            # 旧版 PRD（v1.0, v2.0, bug-fix-handoff）
├── src/
│   ├── config.py           # 配置中心：竞品列表、机构池、DB 路径
│   ├── collector.py        # 数据采集：filing + financials
│   ├── summarizer.py       # LLM 摘要 + EC 纪要
│   ├── dashboard.py        # Streamlit 看板（7 区块）
│   ├── scheduler.py        # 定时任务调度
│   ├── insider_tracker.py  # Form 4/144 内部人交易
│   ├── institutional_tracker.py  # 13F 机构持仓反向查询
│   └── cross_holding.py    # 交叉持股分析引擎
├── data/
│   └── competitor_intel.db # SQLite 数据库
├── utils/
│   └── sec_api_cheatsheet.py  # SEC API 探索记录
├── reference/              # 甲方参考材料
├── output/                 # 报告导出目录
└── requirements.txt        # edgartools, openai, pandas, plotly, streamlit, schedule
```

## 常用命令

```bash
# 启动看板
streamlit run src/dashboard.py

# 运行调度器
python src/scheduler.py

# 运行数据采集
python src/collector.py

# 生成交叉持股报告
python src/cross_holding.py

# 查看数据库
sqlite3 data/competitor_intel.db
```

## 关键注意事项

### SEC / edgartools
- **CIK 必须是 10 位 padded 格式**（如 `0001690820`，不是 `1690820`）
- `EDGAR_IDENTITY` 必须设为有效邮箱，否则 SEC 返回 404
- `Company.get_financials().income_statement()` **只给年度数据**，季度数据要从单个 10-Q 的 `f.xbrl().find_statement("IncomeStatement")` 取
- edgartools **没有** `get_financial_metrics()` 方法
- 8-K items 通过 `f.items` 获取（不是 `f.eightk.items`）
- edgartools **没有** `insider_transactions()` 方法 — Form 4 需要从 XML 解析
- SEC 限流：机构间 `time.sleep(0.15)`

### 13F / 交叉持股
- 13F 滞后 45 天 — 所有报告必须标注 report_period
- `cross_holding_matrix` 是缓存表，每次 13F 采集后重建
- `institutional_holdings` UNIQUE(institution_cik, report_period, ticker)
- Initiation/Liquidation 必须加 `HAVING SUM(value_x1000) > 0` 过滤零值
- QoQ/Turnover 计算必须排除 prev-only 机构（无 curr 数据则无意义）
- FMR CIK `0000938836` 可能已变更，返回 404 需人工确认

### 竞品特殊处理
- UXIN (优信) 和 ATHM (汽车之家) 是 ADR，不受 Section 16 管辖，Form 4/144 极少
- 中概股可能使用 IFRS XBRL 标签，与 US GAAP 不同
- Dashboard 对 UXIN/ATHM 的 Form 4/144 显示"不适用"而非"无数据"

### 投资风格
- 仅 3 类简化标签（Index/Active/Broker），不是 IHS Markit 的 12 类
- Activist 是静态名单 8 家，不是基于 13D 追踪
- 所有对外报告必须加免责声明标注覆盖范围限制

## 当前状态（2026-06-24）

- ✅ Phase 1 + Phase 2 + 模块 F 代码层完成
- ✅ **竞品扩展 5→17 家**（CVNA/KMX/AN/LAD/PAG/GPI/SAH/ABG/CARG/CARS/TRUE/KAR/ACVA/RUSHA/UXIN/ATHM/VRM）
- ✅ **机构 R1 CIK 大规模修正**（12 家错误 CIK 已纠正）
- ✅ **机构 R2 CIK 补全**（新增 Baillie Gifford/Geode/Nuveen/AllianceBernstein/LSV/Legal & General，共 31 家）
- ✅ **13F 数据重抓**：4,575 条持仓 × 4 期对比（2025-06-30 ~ 2026-03-31）
- ✅ **报告模板升级**：封面页 + 动态 17 列 + P3 机构汇总格式
- 🔴 **2026-06-19 对抗性审计发现 5 个 Critical 数据问题**（B1-B5），见 `prd/reference/audit-2026-06-19.md`
- ✅ **T1 金标准 fixture**（7 条）已建，real-time verify 7/7 通过
- ⏳ **T2-T4**（SEC Adapter 防腐层 + 重跑 + 修显示）— 暂停，等 T0 客户新需求处理
- 📋 **T0 客户新需求**（HTML 原型 → 结构化需求）：`prd/client-requirements-2026-06-23.md`
- 📋 **修复路线图**：`docs/repair-plan-2026-06-19.md`

**关键未解决问题（Critical）**：
- B1: institutional_holdings.value_x1000 列实际混存 $ 和 $1000s（SEC 13F 单位不统一）
- B2: cross_holding._fmt_m() 假设输入是 $1000s，致报告显示放大 1000×（Vanguard CVNA 显示 $7T 而非 $7B）
- B3: 因 B1+B2 排名错乱
- B4: Vanguard 假清仓（Q1 2026 13F 未提交，但被 QoQ 判为清仓 $11.8B）
- B5: BlackRock CIK 错误（0001364742 → 应为 0002012383）

详细修复计划见 `docs/repair-plan-2026-06-19.md` 5 任务窗口方案。
