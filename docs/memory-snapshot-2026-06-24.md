# 项目记忆快照（2026-06-24）

> **用途**：在新电脑 / 新 Claude Code 会话中恢复项目全部关键上下文。
>
> **来源**：从 `~/.claude/projects/-Users-liuming-sec/memory/` 下 4 个 memory 文件合并整理。
> 当 Claude Code 在新会话中读到这份文档时，等同于读完了 memory 体系。

---

## 如何使用本文档

### 场景 A：换到新电脑继续这个项目
1. 克隆 repo：`git clone git@github.com:thaddeus-growth/investment-intel-car.git`
2. 在 Claude Code 中**第一个新对话**的 prompt 里说：
   > "我接手了 /Users/liuming/sec 这个项目，请读 docs/memory-snapshot-2026-06-24.md 了解全部上下文"
3. Claude Code 读完这一份文档 = 加载了原 memory 体系的全部内容
4. 之后可以无缝接 T2 / T3 / T4 任何一个任务

### 场景 B：开新对话做新任务
在任何新对话的 prompt 里贴一句：
> "请读 /Users/liuming/sec/docs/memory-snapshot-2026-06-24.md"
Claude Code 立即恢复完整上下文。

### 场景 C：分享给同事
同事只需要这一份 `.md` + 代码本身，就能在 5 分钟内理解整个项目状态。

---

## §1 2026-06-19 对抗性审计 — 9 个数据问题（B1-B9）

**结论：当前报告不可向甲方交付。** 报告显示 Vanguard 卖出 $11.8 万亿、FMR 持有 $3.76 万亿，比全球股票总市值还大。

### Critical（5 个，必修）

| 编号 | 问题 | 根因 | 修复 |
|------|------|------|------|
| **B1** | 混合单位存储：value_x1000 列混存 $ 和 $1000s | edgartools 忠实返回 SEC `<value>` 原始值，SEC 单位不统一 | `_normalize_value(value, shares)`：隐含价 < $1 → ×1000 |
| **B2** | 显示放大 1000× | `_fmt_m()` 做 v/1000 标 $M（错假设输入是 $1000s）| 配合 B1 归一化为美元后改 v/1_000_000 |
| **B3** | 排名错乱 | B1+B2 双重错误 | 修 B1+B2 后自动恢复 |
| **B4** | Vanguard 假清仓 | Q1 2026 13F 未提交，但被 QoQ 判清仓 $11.8B | QoQ 按机构各自最近 2 期对比 |
| **B5** | BlackRock CIK 错误 | 当前 0001364742 = 子公司，2024 起停报 | 改 CIK 为 **0002012383**（BlackRock, Inc.）|

### Major（4 个）

- **B6**：7 家零数据（JPMorgan/BofA/Franklin/ValueAct/Pershing/Berkshire/Gates），CIK 正确但采集失败
- **B7**：Baillie Gifford/Nuveen 周期残缺
- **B8**：Active/Passive 失真（因 B4+B5 Index 缺数据）
- **B9**：QoQ 用全局最新两期，是结构性缺陷

### Minor（8 个，可暂不修）
KAR→OPLN ticker、TRUE 申报停滞、CARG/CARS SIC、RUSHA ticker、免责声明数字、占位邮箱、Commentary 占位、§2/§3 缺列

---

## §2 SEC Adapter 防腐层设计

**为什么需要**：所有 5 个 Critical 根因都在"业务代码直接调 edgartools，没有防御"。edgartools 本身不算有 bug，是 SEC 数据本身脏。

**决策：方案 B，包一层适配器**
```
业务代码 (institutional_tracker.py / cross_holding.py)
    ↓
src/sec_adapter.py  ← 新增防腐层
    ↓
edgartools + SEC JSON API（直接调）
```

**Adapter 契约**：

1. `verify_institution_cik(cik) -> InstitutionMeta`
   - 调 `https://data.sec.gov/submissions/CIK{padded}.json` 拿真实 entity name
   - 检查近 2 年是否有 13F-HR 申报
   - CIK 错就 raise `InvalidCIKError`

2. `fetch_13f_holdings(cik, periods=4) -> List[Holding]`
   - 包住 edgartools `ThirteenF()` 的 `NoneType` / `AttributeError` / `TypeError` 异常
   - 每条 holding 出口前调 `_normalize_value()`
   - 失败 accession_number 必须 log warning 但不抛

3. `_normalize_value(value, shares, ticker) -> int` — 核心算法：
   ```python
   implied_price = value / shares
   if implied_price < 1.0:    return int(value * 1000)   # $1000s 单位
   elif implied_price <= 1000: return int(value)          # 美元
   else: raise SuspiciousValueError(...)
   ```

4. `sanity_check_holding(h) -> List[str]`
   - `value_usd > 20_000_000_000` ($20B) → 报警
   - `value_usd < 1` → 报警
   - `shares <= 0` → 报警

**关键 dataclass**：`Holding`（cik, name, ticker, value_usd, shares, report_period, accession_number）

**工时**：~半天，约 200 行 Python + 50 行测试。

---

## §3 CIK 主清单（31 家机构 + 17 家竞品，审计后状态）

### 31 家种子机构

✅ = 审计已确认正确  🔴 = 需修复

| 状态 | CIK | 标签 | SEC entity 实际名 |
|------|-----|------|------------------|
| ✅ | `0000102909` | Vanguard Group | VANGUARD GROUP INC（Q1 2026 13F 未提交）|
| 🔴⏳ | `0001364742` | BlackRock | **错** — 改 `0002012383` BlackRock, Inc. |
| ✅ | `0000093751` | State Street | STATE STREET CORP |
| ✅ | `0000073124` | Northern Trust | NORTHERN TRUST CORP |
| ✅ | `0001214717` | Geode Capital Management | GEODE CAPITAL MANAGEMENT, LLC |
| ✅ | `0000895421` | Morgan Stanley | MORGAN STANLEY |
| ✅ | `0000886982` | Goldman Sachs Group | GOLDMAN SACHS GROUP INC |
| ✅ | `0000315066` | FMR LLC (Fidelity) | FMR LLC（旧 0000938836 已 404）|
| ✅ | `0000080255` | T. Rowe Price Associates | PRICE T ROWE ASSOCIATES INC /MD/ ⚠ value 单位 $1000s |
| ✅ | `0000354204` | Dimensional Fund Advisors | DIMENSIONAL FUND ADVISORS LP |
| ✅ | `0000914208` | Invesco | Invesco Ltd. |
| ✅ | `0000820027` | Ameriprise Financial | AMERIPRISE FINANCIAL INC ⏳ 零数据 |
| ✅ | `0000038777` | Franklin Resources | FRANKLIN RESOURCES INC ⏳ 零数据 |
| ✅ | `0000070858` | Bank of America | BANK OF AMERICA CORP /DE/ ⏳ 零数据 |
| ✅ | `0000019617` | JPMorgan Chase | JPMORGAN CHASE & CO ⏳ 零数据 |
| ✅ | `0001088875` | Baillie Gifford & Co | BAILLIE GIFFORD & CO（仅 2025-06 一期）|
| ✅ | `0001521019` | Nuveen Asset Management | Nuveen Asset Management, LLC |
| ✅ | `0001109448` | AllianceBernstein | ALLIANCEBERNSTEIN L.P. |
| ✅ | `0001050470` | LSV Asset Management | LSV ASSET MANAGEMENT ⚠ value 单位 $1000s |
| ✅ | `0000764068` | Legal & General Group | Legal & General Group Plc |
| ✅ | `0001037389` | Renaissance Technologies | RENAISSANCE TECHNOLOGIES LLC |
| ✅ | `0001423053` | Citadel Advisors | CITADEL ADVISORS LLC |
| ✅ | `0001179392` | Two Sigma Investments | TWO SIGMA INVESTMENTS, LP |
| ✅ | `0000902219` | Wellington Management | WELLINGTON MANAGEMENT GROUP LLP |
| ✅ | `0001350694` | Bridgewater Associates | Bridgewater Associates, LP |
| ✅ | `0001103804` | Viking Global Investors | VIKING GLOBAL INVESTORS LP |
| ✅ | `0001061165` | Lone Pine Capital | LONE PINE CAPITAL LLC |
| ✅⏳ | `0001418814` | ValueAct Holdings | ValueAct Holdings, L.P. ⏳ 零数据 |
| ✅⏳ | `0001336528` | Pershing Square Capital | Pershing Square Capital Management, L.P. ⏳ 零数据 |
| ✅⏳ | `0001067983` | Berkshire Hathaway | BERKSHIRE HATHAWAY INC ⏳ 零数据 |
| ✅⏳ | `0001166559` | Bill & Melinda Gates Foundation Trust | GATES FOUNDATION TRUST ⏳ 零数据 |

### 17 家竞品（CIK 全部正确）

| Ticker | CIK | 备注 |
|--------|-----|------|
| CVNA / KMX / AN / LAD / PAG / GPI / SAH / ABG | 各自 | 全部正确 |
| CARG / CARS | 1494259 / 1683606 | ⚠ config SIC 标 7370 实际 7374 |
| KAR | 1395942 | ⚠ ticker 已改 OPLN（OPENLANE 2025-01 更名）|
| TRUE | 1327318 | ⚠ 申报停滞，无 2026 申报（疑似私有化）|
| RUSHA | 1012019 | ⚠ A 类，主流是 RUSHB |
| ACVA / VRM | 1637873 / 1580864 | 正确 |
| UXIN / ATHM | 1729173 / 1527636 | ✅ 外国私人发行人（报 20-F）|

### SEC 13F `<value>` 单位规则
- 2022 年起规则改**美元**（精确到 $1）
- 但 T. Rowe Price / LSV 等**仍按 $1000s 填**（SEC 不强制校验）
- 解决：adapter 出口用"隐含股价 = value/shares"判断

---

## §4 金标准 Fixture 设计规范

### 选样原则（6-8 条）
- **大美元单位机构**（必含 3）：Vanguard CVNA 2025-12-31、FMR CVNA 2026-03-31、State Street CVNA 2025-12-31
- **小美元单位机构**（必含 2）：T. Rowe Price、LSV 任何持仓
- **小机构小持仓**（必含 1）
- **跨季度同标的**（必含 1）

### 每条 fixture 字段
```yaml
- id: "vanguard-cvna-2025q4"
  institution_name: "Vanguard Group"
  institution_cik: "0000102909"
  ticker: "CVNA"
  report_period: "2025-12-31"
  expected_value_usd: 7082804283       # 归一化后美元
  expected_shares: 16783101            # 6 个 sub-advisor 子行求和
  tolerance_pct: 0.01                  # 1%
  source_url: "https://www.sec.gov/Archives/edgar/data/..."
  verified_at: "2026-06-19"
```

### 已建好的 7 条（T1 完成）
| id | 场景 | 单位 | value_usd |
|----|------|------|-----------|
| vanguard-cvna-2025q4 | 大·6 子行求和 | USD | $7,082,804,283 |
| vanguard-cvna-2025q3 | 跨季度 QoQ | USD | $5,075,758,918 |
| fmr-cvna-2026q1 | 大·最新期 | USD | $2,341,129,769 |
| state-street-cvna-2025q4 | 大·单行申报 | USD | $2,411,751,034 |
| troweprice-cvna-2026q1 | **小 $1000s（B1 关键）** | $1000s | $5,590,214,000 |
| lsv-an-2026q1 | 小 $1000s·不同标的 | $1000s | $97,193,000 |
| citadel-sah-2026q1 | 小持仓·过度归一化守卫 | USD | $465,247 |

7/7 verify_fixtures.py 实时复验通过（Δ 0.000%）

### SEC XML 抓取流程
1. `https://data.sec.gov/submissions/CIK{padded}.json` 拿 entity 名 + 申报列表
2. 找最近 13F-HR 的 accessionNumber
3. 从 archive index 拿 infotable.xml 路径
4. 解析 XML 找对应 ticker 的 `<infoTable>`，读 `<value>` 和 `<shrsOrPrnAmt><sshPrnamt>`
5. **判断单位**：`value/shares < $1` → ×1000；`$1-$1000` → 已是美元

---

## §5 项目当前状态（2026-06-24）

- ✅ Phase 1 + Phase 2 + 模块 F 代码层完成
- ✅ 竞品扩展 5→17 家
- ✅ 机构 R1/R2 CIK 修正（31 家）
- ✅ 13F 数据采集 4 期对比
- ✅ 报告模板升级（P3 机构汇总 + 动态 17 列 + 封面）
- ✅ T1 金标准 fixture（7/7 VALID）
- ✅ src/sec_adapter.py 已写（待 T3 接入 institutional_tracker.py）
- 🔴 **待修复**：B1-B5 Critical（见 §1）
- 📋 **T0 客户新需求**（HTML 原型）：`prd/client-requirements-2026-06-23.md`
- 📋 **修复路线图**：`docs/repair-plan-2026-06-19.md`

### 任务状态
| 任务 | 状态 |
|------|------|
| T0 客户需求提取 | ✅ 完成 |
| T1 金标准 fixture | ✅ 完成（7/7 VALID）|
| T2 SEC Adapter 防腐层 | ✅ 代码已写，单元测试待补 |
| T3 迁移采集器 + 重跑 13F | ⏳ 待启动 |
| T4 修报告显示层 | ⏳ 待启动 |
| T5 对抗性最终审计 | ⏳ 待启动 |
| T6+ 按客户新需求重设计 | 📋 暂停，等客户确认 |

---

## §6 修复路线图（5 任务窗口 + 1 新增）

完整 prompt 见 `docs/repair-plan-2026-06-19.md`。每个任务在前一个完成前不应启动。

| 任务 | 目的 | 关键产出 | 工时 |
|------|------|---------|------|
| **T1** ✅ | 建金标准 fixture | tests/fixtures/ | 30-60min |
| **T2** ✅ | SEC Adapter 防腐层 | src/sec_adapter.py + tests | 半天 |
| **T3** | 迁移采集器 + 重跑 13F | 数据清洗 + fixture 通过 | 2-3h |
| **T4** | 修报告显示层 | _fmt_m 改 + 重新生成报告 | 1h |
| **T5** | 对抗性最终审计 | 新对话做，确认 Critical=0 | 30min |
| **T0** ✅ | 客户新需求提取 | prd/client-requirements-2026-06-23.md | 1-2h |

---

## §7 关键技术决策记录

### 1. 不完全自建 edgartools
- **不选**完全自建：~3000 行，2 周工期，独自踩 SEC API 坑
- **不选**继续裸调：数据脏无法验证
- **选**包一层 adapter：~200 行，半天完工，保留 edgartools 80% 便利性

### 2. 投资风格分类简化
- IHS Markit 用 12 类（Value/Growth/GARP/Aggressive Growth/Deep Value/Specialty/Alternative/Index/Broker/Yield/Venture Capital/Private Equity）
- 我们简化为 3 类（Index/Active/Broker）
- 已知差距，报告中加免责声明

### 3. ADR 竞品特殊处理
- UXIN (优信) / ATHM (汽车之家) 不受 Section 16 管辖，Form 4/144 极少
- Dashboard 显示"不适用"而非"无数据"

### 4. DB 部署策略
- data/*.db 在 .gitignore
- 但 force-track 当前 DB snapshot 用于 Streamlit Cloud 部署
- T3 修复后会生成新 snapshot 替换

---

## §8 17 家竞品全部 CIK（速查表）

| Ticker | CIK | 名称 | 赛道 |
|--------|-----|------|------|
| CVNA | 0001690820 | Carvana | 线上二手车 |
| KMX | 0001170010 | CarMax | 二手车零售商 |
| AN | 0000350698 | AutoNation | 经销商集团 |
| LAD | 0001023128 | Lithia Motors | 经销商集团 |
| PAG | 0001019849 | Penske Automotive | 经销商集团 |
| GPI | 0001031203 | Group 1 Automotive | 经销商集团 |
| SAH | 0001043509 | Sonic Automotive | 经销商集团 |
| ABG | 0001144980 | Asbury Automotive | 经销商集团 |
| CARG | 0001494259 | CarGurus | 线上平台 |
| CARS | 0001683606 | Cars.com | 线上平台 |
| TRUE | 0001327318 | TrueCar | 线上平台 |
| KAR | 0001395942 | OPENLANE | 批发拍卖 |
| ACVA | 0001637873 | ACV Auctions | 批发拍卖 |
| RUSHA | 0001012019 | Rush Enterprises | 商用车 |
| UXIN | 0001729173 | Uxin (优信) | 中国 ADR |
| ATHM | 0001527636 | Autohome (汽车之家) | 中国 ADR |
| VRM | 0001580864 | Vroom | 线上 (濒临退市) |

---

## §9 客户信息（仅项目内部使用）

- **客户**：瓜子二手车（中国二手车交易平台，准备上市）
- **数据源**：SEC / HKEXnews / 官方披露 + 媒体 / 公众号（公开）
- **业务模型**：线上 + 线下二手车交易，类似 Carvana + Carmax 混合
- **对标要求**：17 家美股已覆盖汽车零售生态全谱
- **客户新模版**（2026-06-17）：`prd/client-requirements-2026-06-23.md`（已提取需求）
- ⚠ **不要在对外产出物中提及"瓜子"这个品牌名**

---

## §10 验证流程

每次代码改动后，验证步骤：
1. `python3 tests/fixtures/verify_fixtures.py` → 应 7/7 VALID
2. `python3 -c "from cross_holding import _get_db, generate_cross_holding_report; ..."` → 跑报告生成
3. 肉眼检查 Top 3 holders 金额应在合理范围：
   - Vanguard CVNA ≈ $7B
   - FMR CVNA ≈ $2.3B
   - Citadel CVNA ≈ $2.1B
4. 检查 "Liquidations" 表不应被单一机构霸占（如 B4 的 Vanguard 假清仓）
