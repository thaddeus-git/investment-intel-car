# 金标准 Fixture — 13F 持仓归一化基准

本目录的 `ground_truth.yaml` 是一套「已知正确」的 13F 持仓记录，用来给
`src/sec_adapter.py`（待建防腐层，见 [[sec-adapter-design]]）和 `institutional_tracker.py`
的数据采集做**自动回归**。每次改采集/归一化代码，都必须先跑 `verify_fixtures.py`
确认这些「应该正确」的记录仍然正确，才能继续。

**为什么需要**：2026-06-19 对抗性审计（[[audit-2026-06-19-critical-issues]]）发现
Vanguard 显示卖出 $11.8 万亿、FMR 持有 $3.76 万亿，根因是 `<value>` 单位混存（B1）
+ 显示放大 1000×（B2）。没有金标准，「修对了」只是主观感觉。

---

## 文件清单

| 文件 | 作用 |
|------|------|
| `ground_truth.yaml` | 7 条 fixture + sanity_bounds |
| `verify_fixtures.py` | 从 SEC 实时重抓，对比 fixture 是否仍成立 |
| `_fetch.py` | SEC 抓取/解析工具模块（被 verify_fixtures.py import） |

---

## 7 条 fixture 一览

| id | 机构 | 标的 | 报告期 | 场景 | 单位 | value_usd | implied 价 |
|----|------|------|--------|------|------|-----------|-----------|
| vanguard-cvna-2025q4 | Vanguard | CVNA | 2025-12-31 | 大单位·6 子行求和 | USD | $7,082,804,283 | $422.02 |
| vanguard-cvna-2025q3 | Vanguard | CVNA | 2025-09-30 | 跨季度 QoQ（配对 Q4） | USD | $5,075,758,918 | $377.24 |
| fmr-cvna-2026q1 | FMR/Fidelity | CVNA | 2026-03-31 | 大单位·最新期 | USD | $2,341,129,769 | $314.38 |
| state-street-cvna-2025q4 | State Street | CVNA | 2025-12-31 | 大单位·单行申报 | USD | $2,411,751,034 | $422.02 |
| troweprice-cvna-2026q1 | T. Rowe Price | CVNA | 2026-03-31 | **小单位 $1000s（B1 关键）** | $1000s | $5,590,214,000 | $0.314→$314 |
| lsv-an-2026q1 | LSV | AN | 2026-03-31 | 小单位 $1000s·不同标的 | $1000s | $97,193,000 | $0.195→$195 |
| citadel-sah-2026q1 | Citadel | SAH | 2026-03-31 | 小持仓·过度归一化守卫 | USD | $465,247 | $68.57 |

**覆盖核对**（满足 [[ground-truth-fixture-spec]] 要求）：
- 大美元单位机构 ≥3 ✅（4 条：vanguard-q4/q3、fmr、state-street）
- 小美元单位机构 ≥2 ✅（troweprice、lsv）
- 跨季度同机构同标的 1 ✅（vanguard q3↔q4）
- 小机构小持仓（不过度归一化）1 ✅（citadel-sah）

---

## 来源 URL（每条 fixture 的 SEC 原始 infotable.xml）

| id | source_url |
|----|-----------|
| vanguard-cvna-2025q4 | https://www.sec.gov/Archives/edgar/data/102909/000010290926000031/13F_0000102909_20251231.xml |
| vanguard-cvna-2025q3 | https://www.sec.gov/Archives/edgar/data/102909/000010290925000353/13F_0000102909_20250930.xml |
| fmr-cvna-2026q1 | https://www.sec.gov/Archives/edgar/data/315066/000031506626001390/20260515_FMRLLC.xml |
| state-street-cvna-2025q4 | https://www.sec.gov/Archives/edgar/data/93751/000009375126000100/XML_Infotable.xml |
| troweprice-cvna-2026q1 | https://www.sec.gov/Archives/edgar/data/80255/000008025526000381/infotable.xml |
| lsv-an-2026q1 | https://www.sec.gov/Archives/edgar/data/1050470/000105047026000004/Holdings20260511.xml |
| citadel-sah-2026q1 | https://www.sec.gov/Archives/edgar/data/1423053/000110465926062477/infotable.xml |

accession 号见 `ground_truth.yaml` 各条 `source_accession`。

---

## 验证方法

### 1. 取值流程（建 fixture 时，2026-06-19）

每条 `expected_value_usd` / `expected_shares` 由 agent **直接从 SEC 原始 XML 读出**，
绝不由本项目 DB 拷贝（DB 本身是要验证的对象）：

1. `https://data.sec.gov/submissions/CIK{padded10}.json` → 找目标报告期的 13F-HR accession
2. `https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json` → 找 infotable.xml 文件名
3. 解析 XML，按 ticker 的 `nameOfIssuer`/`cusip` 匹配**所有** `<infoTable>` 子行
4. **求和所有 SH 子行**的 `<value>` 和 `<sshPrnamt>`（同一标的常被 sub-advisor 拆成多行；
   见 [[fmr-cik-change]] 的「多行去重丢值 bug」）
5. **单位判断**：`implied = value_sum / shares_sum`
   - implied < $1 → `<value>` 是 $1000s，`value_usd = value_sum × 1000`
   - implied $1–$1000 → 已是美元，`value_usd = value_sum`
6. 交叉验证：归一化后的 implied 价应 ≈ 该标的该报告期真实收盘价

### 2. 交叉验证（强证据，证明解析无误）

同一报告期、同一标的，跨多家机构的 implied 价应高度一致：

- **CVNA 2026-03-31 → $314.38**：FMR、Renaissance、Viking、Lone Pine、Citadel 五家
  infotable 独立算出完全相同的 $314.38（T. Rowe Price 的 $1000s 原始 implied $0.31438 ×1000 也=$314.38）。
- **CVNA 2025-12-31 → $422.02**：Vanguard、State Street 一致。
- **AN 2026-03-31 → $195.26**：LSV（$1000s）、Bridgewater、Citadel 一致。

这种跨机构一致性排除了「单家解析错」的可能。

### 3. 实时复验

```bash
python3 tests/fixtures/verify_fixtures.py            # 简洁
python3 tests/fixtures/verify_fixtures.py --verbose  # 打印每条子行明细
python3 tests/fixtures/verify_fixtures.py --only vanguard-cvna-2025q4,troweprice-cvna-2026q1
```

退出码 0 = 全部仍有效；1 = 有 fixture 值变了 / 抓不到（期变了不算失败，见下）。

**2026-06-19 建库时复验结果：7/7 VALID，Δ 0.000%。**

---

## 已知过期风险

13F fixture 会随时间失效。各条风险：

| id | 过期风险 | 时效 |
|----|---------|------|
| vanguard-cvna-2025q4 | Vanguard 尚未提交 Q1 2026 (2026-03-31) 13F（审计 B4 已记录）；一旦提交，本条仍代表 2025-12-31 历史真相，不会变，但不再是「最新期」 | 稳定（历史期不会被改） |
| vanguard-cvna-2025q3 | 同上；历史期稳定 | 稳定 |
| fmr-cvna-2026q1 | FMR 已提交到 2026-03-31；下一期（2026-06-30）预计 2026-08 中旬提交，届时本条变「次新」但仍有效 | ~2 个月后变次新 |
| state-street-cvna-2025q4 | 同 fmr，State Street 已提交到 2026-03-31 | ~2 个月后变次新 |
| troweprice-cvna-2026q1 | 同 fmr | ~2 个月后变次新 |
| lsv-an-2026q1 | 同 fmr | ~2 个月后变次新 |
| citadel-sah-2026q1 | Citadel 持仓变动频繁（量化基金），SAH 小持仓可能下期被清仓；但本条锚定的是 2026-03-31 这一期，历史不变 | 历史稳定；持仓可能下期消失 |

**重要**：`verify_fixtures.py` 区分两种「变」：
- `VALUE_CHANGED` / `FETCH_ERROR` → 真·失效（filing 被修订、数值对不上、抓不到）→ 必须查
- `PERIOD_CHANGED` → accession 滚出最近窗口或机构提交了更新期 → fixture 仍是有效历史基准，
  不是 bug；如要追踪最新期，新增一条 fixture 即可

历史 13F 一旦归档，SEC 通常不修订，所以这 7 条作为「单位归一化回归基准」长期有效。
真正需要定期续命的是「追踪最新期」的用途——那应在 adapter 建好后用 CI 定期跑 verify。

---

## SEC 访问注意

- **User-Agent 必须含邮箱**：本目录脚本用 `thaddeus thaddeus@example.com`
  （⚠️ 这是占位邮箱，见审计 Minor 项；正式交付前应换成真实邮箱）
- **限流**：`_fetch.py` 内置 0.3s 间隔 + 指数退避（1/2/4/8s）。SEC `data.sec.gov`
  对过快请求会 reset 连接，本目录脚本已处理。
- 偶发 `SSLEOFError` / `Connection reset` 是 SEC 侧限流，重试即可恢复（已观测到整站
  短暂不可用 ~1 分钟后自愈）。

---

## 与 adapter 的对接（T2 任务建）

`tests/test_fixtures.py`（待建）应做：

```python
def test_golden_holdings(adapter, db):
    for golden in load_fixtures("tests/fixtures/ground_truth.yaml"):
        actual = db.execute(
            "SELECT value_usd, shares FROM institutional_holdings "
            "WHERE institution_cik=? AND ticker=? AND report_period=?",
            (golden["institution_cik"], golden["ticker"], golden["report_period"])
        ).fetchone()
        assert actual, f"{golden['id']}: DB 无对应记录"
        assert abs(actual.value_usd - golden["expected_value_usd"]) / golden["expected_value_usd"] < golden["tolerance_pct"]
        assert abs(actual.shares - golden["expected_shares"]) / golden["expected_shares"] < golden["tolerance_pct"]
```

其中 `adapter` 是 [[sec-adapter-design]] 定义的防腐层，`db` 是采集后的 SQLite。
