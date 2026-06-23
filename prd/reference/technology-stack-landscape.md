# 技术栈全景对比

> 目的：确认每一项技术选型是当前最优解，明确「用」、「不用的理由」、「替代时机」。
>
> 生成日期：2026-06-18

---

## 1. SEC 数据底座

| 方案 | 类型 | 覆盖 | 状态 |
|------|------|------|------|
| **[edgartools](https://github.com/dgunning/edgartools)** | 开源 Python (MIT) | 20+ form 类型、XBRL、13F | ✅ **在用** |
| [edgar-parser](https://github.com/henrysouchien/edgar-parser) | 开源 Python | 8-K LLM 提取、Earnings Call | ✅ **在用**（`[llm]` extra） |
| [sec-api.io](https://sec-api.io) | 付费 API | 全类型 filing、XBRL、Insider、13F | ❌ 付费，对标 edgartools 免费能力 |
| [OpenBB](https://openbb.co) | 开源平台 | SEC 插件（可能底层也是 edgartools） | ❌ 过重，平台级而非库级 |
| [sec-parser](https://github.com/alphanome-ai/sec-parser) | 开源 Python | 轻量 SEC 解析 | ❌ 不如 edgartools 成熟 |
| [Arelle](https://arelle.org) | 开源 | XBRL 校验器 | ❌ 重量级，不适合嵌入流水线 |
| Raw SEC EDGAR API | 免费 | 原始数据 | ❌ 无 XBRL 标准化，这正是 edgartools 的价值 |

**判断**：edgartools 是该赛道最佳选择。PRD 附录 A 已验证过，无需改动。

---

## 2. 财务数据提取（XBRL → 结构化指标）

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **edgartools `render_statement().to_dataframe()`** | 内置 | 标准化 IS/BS/CF | 各公司 XBRL 标签可能有差异 |
| [Calcbench](https://www.calcbench.com) | 付费 API | 完全标准化的财务指标 | $15K+/年 |
| [Financial Modeling Prep](https://financialmodelingprep.com) | 付费 API | 财务 + transcript | $300+/年 |
| [Polygon.io](https://polygon.io) | 付费 API | 实时 + 财务 | 财务数据不如 SEC 权威 |

**判断**：✅ edgartools XBRL + `METRIC_TAGS` 映射表是最优解。12 个核心指标覆盖充分。

---

## 3. 内部人交易（Form 4 / Form 144）

| 方案 | 类型 | 覆盖 | 状态 |
|------|------|------|------|
| **自定义 XML 正则解析** | 自建 | Form 4 XML + Form 144 文本 | ✅ **在用**（`insider_tracker.py`） |
| [OpenInsider](http://openinsider.com) | 免费网站 | Form 4 数据 | ❌ 无 API，靠爬虫 |
| [sec-api.io Insider Trading](https://sec-api.io) | 付费 API | 结构化 Form 3/4/5 | ❌ $79+/月 |
| [insider-open](https://pypi.org/project/insider-open/) | 开源 PyPI | Form 4 解析 | ⚠️ 待验证覆盖度 |

**判断**：edgartools 明确没有 `insider_transactions()`（CLAUSE.md 已记录），自建是必要之举。如果 Form 4 XML 结构发生变动导致解析失败频率 > 5%，考虑：
1. 先评估 [insider-open](https://pypi.org/project/insider-open/) 能否替换（0 成本）
2. 再考虑 sec-api.io 付费 API（覆盖完整，无需维护解析逻辑）

---

## 4. 机构持仓（13F 反向查询）

| 方案 | 类型 | 覆盖 | 状态 |
|------|------|------|------|
| **edgartools `ThirteenF` + 种子列表** | 自建逻辑 | 25 家种子机构 | ✅ **在用** |
| [WhaleWisdom](https://whalewisdom.com) | 付费 API | 5,000+ 机构，自带分析 | ❌ $500+/月 |
| [Fintel 13F](https://fintel.io) | 付费 | 机构持仓 + 变动 | ❌ 机构档位 |
| [Daloopa](https://www.daloopa.com) | 付费 | 13F + 基本面 | ❌ 企业定价 |

**判断**：SEC 不支持"反向查询谁持有某股票"是底层限制，无论用什么工具都必须维护种子列表。edgartools `ThirteenF.infotable` 已经处理了 13F XML 解析，我们的代码只是迭代和数据路由（~150 行核心逻辑），合理。

---

## 5. LLM 调用层

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **直接 OpenAI SDK** | 自建 | 零额外依赖，简单 | 与 DeepSeek 紧耦合 |
| [LiteLLM](https://github.com/BerriAI/litellm) | 开源代理 | 100+ 模型统一接口 | 增加依赖 |
| [LangChain](https://langchain.com) | 框架 | 丰富的链/代理抽象 | 重量级，本项目只需简单 chat |
| [Instructor](https://python.useinstructor.com) | 库 | 结构化 JSON 输出 | 本项目不需要 JSON schema |
| [Vercel AI SDK](https://sdk.vercel.ai) | JS/TS | 流式、工具调用 | 不同语言栈 |

**判断**：✅ 直接 OpenAI SDK 调用 DeepSeek 是最简方案。当前只有 3 个 LLM 调用点（8-K 摘要、EC 纪要、Insider 摘要），不构成引入框架的理由。**但需提取公共 `llm_client.py` 消除 `_llm_chat()` 重复**。

---

## 6. 看板 / 可视化

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **[Streamlit](https://streamlit.io)** | 开源 | Python 原生、快速迭代、DataFrame 无缝 | 定制化有限 |
| [Dash (Plotly)](https://dash.plotly.com) | 开源 | 灵活布局、企业级 | 代码量 2-3× |
| [Gradio](https://gradio.app) | 开源 | ML 场景强 | 偏 demo 型 |
| [Panel (HoloViz)](https://panel.holoviz.org) | 开源 | 灵活、组件多 | 社区较小 |
| [Taipy](https://www.taipy.io) | 开源 | 现代、支持大应用 | 较新 |

**判断**：✅ Streamlit 是本场景最优解。分析师需要快速迭代看板，Streamlit 的 `st.cache_data` + Plotly 集成完美匹配需求。Dash 虽然更强大但会让 1200 行变 3000 行，不值得。

---

## 7. 数据库 / 存储

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **[SQLite](https://sqlite.org) + 原生 SQL** | 嵌入式 | 零配置、单文件、便携 | 无并发写 |
| SQLAlchemy + SQLite | ORM | Schema 迁移、连接池 | 增加复杂度，本案 12 张表 |
| [DuckDB](https://duckdb.org) | 嵌入式分析 | 分析查询更快、Parquet 支持 | 另一个依赖，本案数据量 < 100MB |
| PostgreSQL | 服务器 DB | 生产级、并发 | 需要运维 |
| [Polars](https://pola.rs) | DataFrame | 比 pandas 更快 | API 差异，team 熟悉 pandas |

**判断**：✅ SQLite + pandas 是正确选择。数据量小（<100MB）、单用户看板、无并发写需求。SQLAlchemy 目前不值得引入（12 张表、CRUD 简单）。当表数超 20 或需要 migration 时再考虑。

---

## 8. 定时调度

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **[schedule](https://github.com/dbader/schedule)** | Python 库 | 简单、可读 | 纯内存、无持久化、进程退出丢状态 |
| [APScheduler](https://apscheduler.readthedocs.io) | Python 库 | 持久化、cron/interval 双模式 | 配置稍多 |
| Celery | 分布式队列 | 可扩展 | 需要 broker（Redis/RabbitMQ） |
| Prefect / Airflow | 工作流引擎 | 完整流水线 | 对本案严重过度 |
| systemd timer / crontab | 系统级 | 简单可靠 | 与 Python 代码分离 |

**判断**：✅ `schedule` 对 MVP 足够。当项目需要 24×7 无人值守运行时 **升级为 APScheduler**（支持 SQLite 持久化，进程重启不丢调度状态）。

---

## 9. 报告生成

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **字符串拼接** | 自建 | 零依赖 | 难维护、易错、不可复用 |
| [Jinja2](https://jinja.palletsprojects.com) | 模板引擎 | 模板/数据分离、可复用 | 加一个依赖 |
| [WeasyPrint](https://weasyprint.org) | PDF 生成 | HTML → PDF | 需要系统依赖（Cairo） |
| [Quarto](https://quarto.org) | 科学出版 | 学术级报告 | 重量级、R/Python 混合 |
| [Great Tables](https://github.com/posit-dev/great-tables) | 表格格式化 | 漂亮表格、HTML 输出 | 仅覆盖表格 |

**判断**：⚠️ **建议替换为 Jinja2**。`cross_holding.py` 的 `generate_cross_holding_report()` 有 270 行字符串拼接，改用 Jinja2 模板后：
- 代码量减少 ~200 行
- 模板可单独编辑（非程序员也能调格式）
- 为后续 PDF 导出做铺垫（HTML → WeasyPrint）

---

## 10. 数据验证

| 方案 | 类型 | 优势 | 劣势 |
|------|------|------|------|
| **手动验证**（`verify_carvana()`） | 自建 | 简单 | 只有 Carvana、只有 Q1、手动维护预期值 |
| [Pandera](https://pandera.readthedocs.io) | Schema 验证 | DataFrame 级别检查 | 需要学习 DSL |
| [Great Expectations](https://greatexpectations.io) | 数据质量框架 | 完整的质量报告 | 重度 |
| [Pydantic](https://docs.pydantic.dev) | 数据校验 | Python 原生 | 对 DataFrame 不如 Pandera |

**判断**：⚠️ 当前 `verify_carvana()` 太简陋。建议引入 **Pandera** 做 DataFrame schema 校验（列存在性、dtype、值域），配合手动 spot-check 做精度验证。

---

## 11. Earnings Call Transcript 获取

| 方案 | 类型 | 覆盖 | 状态 |
|------|------|------|------|
| **8-K Item 2.02 财报新闻稿** | edgartools | 5 家美股 | ✅ 已有 |
| Seeking Alpha 转录 | 爬虫 | 英文 transcription | ⏳ PRD 标注但未落地 |
| [Financial Modeling Prep](https://financialmodelingprep.com) | 付费 API | Transcript API | ❌ $300+/年 |
| [EarningsCall](https://earningscall.ai) | 付费 API | Transcript + 摘要 | ❌ 企业定价 |
| [Quartr](https://quartr.com) | 付费 | Transcript + 音频 | ❌ 企业定价 |

**判断**：8-K 财报新闻稿已覆盖核心需求。Seeking Alpha 转录是 enhanced feature，不必急于实现。

---

## 总结矩阵

| # | 层 | 当前选型 | 评价 | 行动 |
|---|-----|---------|------|------|
| 1 | SEC 数据 | edgartools + edgar-parser | ✅ 最优 | 无需改动 |
| 2 | XBRL 财务 | edgartools render_statement | ✅ 最优 | 无需改动 |
| 3 | 内部人交易 | 自建 XML 正则解析 | ⚠️ 必要但脆弱 | 考察 insider-open；如不稳定则上 sec-api.io |
| 4 | 机构持仓 | edgartools ThirteenF | ✅ 最优 | 无需改动 |
| 5 | LLM | 直调 DeepSeek (OpenAI SDK) | ✅ 够用 | **提取公共 llm_client.py** |
| 6 | 看板 | Streamlit + Plotly | ✅ 最优 | 无需改动 |
| 7 | 数据库 | SQLite + 原生 SQL | ✅ 够用 | 表 > 20 时考虑 SQLAlchemy |
| 8 | 调度 | schedule | ✅ MVP 够用 | 生产化时换 APScheduler |
| 9 | 报告生成 | 字符串拼接 | ⚠️ 应替换 | **换 Jinja2 模板** |
| 10 | 数据验证 | 手动 verify_carvana | ⚠️ 可改进 | 引入 Pandera |
| 11 | Transcript | 8-K 新闻稿 | ✅ 够用 | SA 转录后续 |

**底线**：没有发现第二个 edgartools 级别的替代机会。当前技术栈整体合理，最大改进点是 Jinja2 报告模板和提取 LLM 公共模块 —— 都属于小规模优化。
