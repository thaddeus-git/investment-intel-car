"""
竞品情报监控系统 — Streamlit 看板

单页，6 个区块：
1. 概览条：5 家公司涨跌信号
2. Filing 时间线
3. 财务对比图（指标下拉 × 5 家公司折线）
4. 事件预警列表
5. EC 纪要
6. 内部人与机构动向（Phase 2 新增）
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# 把 src/ 加入 path 以便 import config
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    COMPETITORS,
    DATABASE_PATH,
    METRIC_LABELS_CN,
    METRIC_CATEGORIES,
)

st.set_page_config(
    page_title="竞品情报监控",
    page_icon="🏠",
    layout="wide",
)


# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════

@st.cache_data(ttl=300)  # 5 分钟缓存
def load_data():
    """从 SQLite 加载所有看板数据。"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row

    filings = pd.read_sql_query("""
        SELECT f.*, c.ticker, c.name_cn
        FROM filings f
        JOIN companies c ON f.company_id = c.id
        ORDER BY f.filing_date DESC
    """, conn)

    financials = pd.read_sql_query("""
        SELECT fin.*, c.ticker, c.name_cn
        FROM financials fin
        JOIN companies c ON fin.company_id = c.id
        ORDER BY fin.fiscal_year, fin.fiscal_quarter
    """, conn)

    events = pd.read_sql_query("""
        SELECT e.*, c.ticker, c.name_cn, f.accession_number
        FROM events e
        JOIN companies c ON e.company_id = c.id
        LEFT JOIN filings f ON e.filing_id = f.id
        ORDER BY e.created_at DESC
    """, conn)

    ec_notes = pd.read_sql_query("""
        SELECT ec.*, c.ticker, c.name_cn
        FROM earnings_call_notes ec
        JOIN companies c ON ec.company_id = c.id
        ORDER BY ec.created_at DESC
    """, conn)

    # Phase 2: 内部人交易 + 机构持仓
    insider_txns = pd.read_sql_query("""
        SELECT it.*, c.ticker, c.name_cn
        FROM insider_transactions it
        JOIN companies c ON it.company_id = c.id
        ORDER BY it.transaction_date DESC
    """, conn)

    form144 = pd.read_sql_query("""
        SELECT f14.*, c.ticker, c.name_cn
        FROM form144_filings f14
        JOIN companies c ON f14.company_id = c.id
        ORDER BY f14.filing_date DESC
    """, conn)

    insider_sent = pd.read_sql_query("""
        SELECT ins.*, c.ticker, c.name_cn
        FROM insider_sentiment ins
        JOIN companies c ON ins.company_id = c.id
        ORDER BY ins.created_at DESC
    """, conn)

    # ih.ticker 已是竞品 ticker，JOIN 只补 name_cn
    inst_holdings = pd.read_sql_query("""
        SELECT ih.*, c.name_cn
        FROM institutional_holdings ih
        JOIN companies c ON ih.company_id = c.id
        ORDER BY ih.report_period DESC, ih.value_x1000 DESC
    """, conn)

    inst_signal = pd.read_sql_query("""
        SELECT isig.*, c.ticker, c.name_cn
        FROM institutional_signal isig
        JOIN companies c ON isig.company_id = c.id
        ORDER BY isig.report_period DESC
    """, conn)

    # 模块 F: 交叉持股矩阵（仅最新报告期）
    cross_matrix = pd.read_sql_query("""
        SELECT * FROM cross_holding_matrix
        WHERE report_period = (SELECT MAX(report_period) FROM cross_holding_matrix)
        ORDER BY total_value_x1000 DESC
    """, conn)

    conn.close()
    return filings, financials, events, ec_notes, insider_txns, form144, insider_sent, inst_holdings, inst_signal, cross_matrix


def latest_update(events_df, filings_df):
    """估算最后更新时间。"""
    ts = None
    if not events_df.empty:
        ts = events_df["created_at"].max()
    if not filings_df.empty:
        f_ts = filings_df["created_at"].max()
        if f_ts and (ts is None or f_ts > ts):
            ts = f_ts
    if ts:
        return str(ts)
    return "暂无"


# load_data() 实际查询的 11 张表 —— 容错预检的期望集合
_EXPECTED_TABLES = (
    "companies", "filings", "financials", "events", "earnings_call_notes",
    "insider_transactions", "form144_filings", "insider_sentiment",
    "institutional_holdings", "institutional_signal", "cross_holding_matrix",
)


def check_db_ready():
    """启动预检：DB 文件存在且表齐全。失败时给清晰中文提示并 stop，避免
    Streamlit Cloud 上 redacted 崩溃页让人无法定位问题。不进缓存。"""
    db_path = Path(DATABASE_PATH)
    if not db_path.exists():
        st.error(
            f"⚠️ 数据库未就绪\n\n"
            f"找不到数据库文件：`{db_path}`\n\n"
            f"**本地**：确认已跑过采集器生成 `data/competitor_intel.db`。\n"
            f"**Streamlit Cloud**：DB 文件需提交进 git（`git add -f data/competitor_intel.db`），"
            f"Cloud 不会保留运行时写入的文件。"
        )
        st.stop()

    conn = sqlite3.connect(DATABASE_PATH)
    try:
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()

    missing = [t for t in _EXPECTED_TABLES if t not in existing]
    if missing:
        st.error(
            f"⚠️ 数据库结构不完整\n\n"
            f"缺失表：`{', '.join(missing)}`\n\n"
            f"请本地重新跑采集器初始化 DB 并重新提交："
            f"`python3 src/collector.py` → `git add -f data/competitor_intel.db` → push。"
        )
        st.stop()


# ═══════════════════════════════════════════════════════════
# UI 组件
# ═══════════════════════════════════════════════════════════

def render_overview_bar(filings_df, events_df):
    """概览条：5 家公司最近信号。"""
    cols = st.columns(5)
    for i, comp in enumerate(COMPETITORS):
        ticker = comp["ticker"]
        name = comp["name_cn"]

        # 最近一周的高风险事件数
        c_events = events_df[events_df["ticker"] == ticker]
        high_count = len(c_events[c_events["severity"] == "high"])

        # 最新 filing
        c_filings = filings_df[filings_df["ticker"] == ticker]
        latest_form = c_filings.iloc[0]["form_type"] if not c_filings.empty else "—"

        emoji = "🔴" if high_count > 0 else "🟢"
        with cols[i]:
            st.metric(
                label=f"{emoji} {name} ({ticker})",
                value=latest_form,
                delta=f"{high_count} 高风险" if high_count > 0 else "平稳",
            )


def render_filing_timeline(filings_df):
    """最新 Filing 时间线。"""
    st.subheader("📰 最新 Filing 时间线")

    # 公司筛选
    all_tickers = [c["ticker"] for c in COMPETITORS]
    selected = st.multiselect(
        "筛选公司",
        options=all_tickers,
        default=all_tickers[:3],
        format_func=lambda t: f"{t} — {next(c['name_cn'] for c in COMPETITORS if c['ticker']==t)}",
        key="timeline_filter",
    )

    if not selected:
        st.info("请选择至少一家公司")
        return

    df = filings_df[filings_df["ticker"].isin(selected)].head(15)

    for _, row in df.iterrows():
        items = row["items"] or ""
        form = row["form_type"]
        date = str(row["filing_date"])[:10]
        emoji = "📊" if form in ("10-Q", "10-K", "20-F") else "⚠️" if form in ("8-K", "6-K") else "📋"

        # 高危事件标红
        is_high = any(
            it.strip().replace("Item ", "") in ("2.02", "5.02", "1.01", "4.01", "4.02")
            for it in items.split(",") if it.strip()
        )

        bg = "#fff5f5" if is_high else "transparent"
        st.markdown(
            f'<div style="background:{bg};padding:4px 8px;margin:2px 0;border-radius:4px">'
            f'<b>{emoji} {date}</b> &nbsp;{row["ticker"]} · {form} &nbsp;'
            f'{f"<span style=color:red>Items: {items}</span>" if is_high else items}'
            f'</div>',
            unsafe_allow_html=True,
        )


def render_financial_chart(financials_df):
    """财务对比折线图。"""
    st.subheader("📈 财务对比")

    if financials_df.empty:
        st.info("暂无财务数据")
        return

    # 按类别组织指标选择
    all_metrics = sorted(METRIC_LABELS_CN.keys())
    default_metrics = ["revenue", "gross_profit", "net_income"]

    col1, col2, col3 = st.columns(3)
    with col1:
        selected_metric = st.selectbox(
            "选择指标",
            options=all_metrics,
            format_func=lambda m: f"{METRIC_LABELS_CN[m]} ({m})",
            index=all_metrics.index("revenue") if "revenue" in all_metrics else 0,
        )
    with col2:
        all_tickers = sorted(financials_df["ticker"].unique())
        selected_tickers = st.multiselect(
            "选择公司",
            options=all_tickers,
            default=list(all_tickers),
            key="chart_filter",
        )
    with col3:
        period_type = st.radio(
            "周期",
            options=["季度", "年度"],
            horizontal=True,
            key="period_type",
        )

    if not selected_tickers:
        st.info("请选择至少一家公司")
        return

    # 过滤数据
    is_quarterly = period_type == "季度"
    df = financials_df[
        (financials_df["metric_name"] == selected_metric)
        & (financials_df["ticker"].isin(selected_tickers))
        & (financials_df["fiscal_quarter"].notna() if is_quarterly else financials_df["fiscal_quarter"].isna())
    ]

    if df.empty:
        st.info(f"该指标暂无{period_type}数据")
        return

    # 构造横轴标签
    if is_quarterly:
        df = df.copy()
        df["period"] = df.apply(
            lambda r: f"{int(r['fiscal_year'])}Q{int(r['fiscal_quarter'])}", axis=1
        )
    else:
        df = df.copy()
        df["period"] = df["fiscal_year"].astype(int).astype(str)

    df = df.sort_values("period")

    # 画图
    fig = px.line(
        df,
        x="period",
        y="metric_value",
        color="ticker",
        markers=True,
        title=f"{METRIC_LABELS_CN.get(selected_metric, selected_metric)} — {period_type}趋势",
        labels={"metric_value": "USD", "period": "", "ticker": ""},
    )
    fig.update_layout(
        height=400,
        margin=dict(l=20, r=20, t=50, b=20),
        hovermode="x unified",
    )
    fig.update_yaxes(tickprefix="$", tickformat=".2s")
    st.plotly_chart(fig, width='stretch')

    # 数据表格
    with st.expander("查看原始数据"):
        pivot = df.pivot_table(
            index="period", columns="ticker", values="metric_value", aggfunc="first"
        )
        st.dataframe(pivot, width='stretch')


def render_event_alerts(events_df):
    """事件预警列表。"""
    st.subheader("⚠️ 事件预警")

    if events_df.empty:
        st.info("暂无事件")
        return

    # 筛选
    severity_map = {"🔴 严重": "high", "🟡 关注": "medium", "🟢 信息": "low", "⚪ 其他": "info"}
    selected_severity = st.multiselect(
        "严重级别",
        options=list(severity_map.keys()),
        default=["🔴 严重", "🟡 关注"],
        key="severity_filter",
    )
    selected_sev_values = [severity_map[s] for s in selected_severity]

    df = events_df[events_df["severity"].isin(selected_sev_values)].head(30)

    for _, row in df.iterrows():
        sev = row["severity"]
        emoji = {"high": "🔴", "medium": "🟡", "low": "🟢", "info": "⚪"}.get(sev, "⚪")
        summary = row["summary_cn"] or "(无摘要)"

        with st.expander(f"{emoji} {row['ticker']} — {row['event_type']}"):
            st.markdown(summary)
            if row["raw_text"]:
                with st.expander("📄 原始文本"):
                    st.text(row["raw_text"][:3000])


def render_ec_notes(ec_df):
    """最新 Earnings Call 纪要。"""
    st.subheader("📝 最新 Earnings Call 纪要")

    if ec_df.empty:
        st.info("暂无纪要")
        return

    for _, row in ec_df.head(6).iterrows():
        fy = int(row["fiscal_year"]) if row["fiscal_year"] else ""
        fq = f"Q{int(row['fiscal_quarter'])}" if row.get("fiscal_quarter") else ""
        period = f"{fy}{fq}" if fy else ""
        title = f"{row['ticker']} {period} {row['source']}"
        with st.expander(title):
            if row["full_text_md"]:
                st.markdown(row["full_text_md"])
            else:
                st.info("暂无内容")


def render_insider_institutional(insider_sent_df, insider_txns_df, form144_df,
                                  inst_signal_df, inst_holdings_df):
    """🏦 内部人与机构动向（Phase 2 新增）。"""
    st.subheader("🏦 内部人与机构动向")

    # ── 内部人情绪指标卡片 ──
    st.markdown("#### 📊 内部人情绪指标（近 3 个月）")

    cols = st.columns(5)
    for i, comp in enumerate(COMPETITORS):
        ticker = comp["ticker"]
        name = comp["name_cn"]
        has_s16 = comp.get("has_section16", True)

        with cols[i]:
            if not has_s16:
                st.metric(
                    label=f"⚪ {name} ({ticker})",
                    value="不适用",
                    delta="无 Section 16 义务",
                    delta_color="off",
                )
            else:
                comp_sent = insider_sent_df[insider_sent_df["ticker"] == ticker]
                if comp_sent.empty:
                    st.metric(
                        label=f"⚪ {name} ({ticker})",
                        value="暂无数据",
                        delta="等待首次采集",
                        delta_color="off",
                    )
                else:
                    latest = comp_sent.iloc[0]
                    score = latest["sentiment_score"]
                    label = latest["sentiment_label"]
                    emoji = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(label, "⚪")
                    label_cn = {"bullish": "看多", "neutral": "中性", "bearish": "看空"}.get(label, "?")
                    st.metric(
                        label=f"{emoji} {name} ({ticker})",
                        value=f"{label_cn} ({score:+.0f})",
                        delta=f"{int(latest['buy_count'])}买 / {int(latest['sell_count'])}卖",
                    )

    st.divider()

    # ── 内部人交易明细 + Form 144 减持计划 ──
    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("#### 📋 最近内部人交易")
        if insider_txns_df.empty:
            st.info("暂无内部人交易数据")
        else:
            # Filter to Section 16 companies only
            s16_tickers = [c["ticker"] for c in COMPETITORS if c.get("has_section16", True)]
            txns_view = insider_txns_df[insider_txns_df["ticker"].isin(s16_tickers)].head(20)

            if txns_view.empty:
                st.info("暂无有 Section 16 义务的公司交易数据")
            else:
                display_cols = {
                    "filing_date": "申报日",
                    "ticker": "公司",
                    "owner_name": "内部人",
                    "transaction_code": "交易",
                    "shares": "股数",
                    "price_per_share": "单价",
                    "total_value": "金额",
                    "transaction_date": "交易日",
                }
                view = txns_view[list(display_cols.keys())].rename(columns=display_cols)
                view["金额"] = view["金额"].apply(
                    lambda x: f"${x:,.0f}" if pd.notna(x) and x > 0 else "-"
                )
                view["单价"] = view["单价"].apply(
                    lambda x: f"${x:,.2f}" if pd.notna(x) and x > 0 else "-"
                )
                view["股数"] = view["股数"].apply(
                    lambda x: f"{x:,.0f}" if pd.notna(x) and x > 0 else "-"
                )
                st.dataframe(view, width='stretch', hide_index=True)

    with col_right:
        st.markdown("#### ⚠️ 近期减持计划 (Form 144)")
        if form144_df.empty:
            st.info("暂无 Form 144 减持计划")
        else:
            s16_tickers = [c["ticker"] for c in COMPETITORS if c.get("has_section16", True)]
            f144_view = form144_df[form144_df["ticker"].isin(s16_tickers)].head(10)

            if f144_view.empty:
                st.info("暂无减持计划")
            else:
                display_cols = {
                    "filing_date": "申报日",
                    "ticker": "公司",
                    "seller_name": "出售人",
                    "shares_to_sell": "计划出售股数",
                    "aggregate_market_value": "预估市值",
                }
                view = f144_view[list(display_cols.keys())].rename(columns=display_cols)
                view["预估市值"] = view["预估市值"].apply(
                    lambda x: f"${x:,.0f}" if pd.notna(x) and x > 0 else "-"
                )
                view["计划出售股数"] = view["计划出售股数"].apply(
                    lambda x: f"{x:,.0f}" if pd.notna(x) and x > 0 else "-"
                )
                st.dataframe(view, width='stretch', hide_index=True)

    st.divider()

    # ── 机构持仓变动 ──
    st.markdown("#### 🏛️ 机构持仓变动 (13F)")

    if inst_signal_df.empty:
        st.info("暂无 13F 机构持仓数据（季报，需等待季末+50天）")
    else:
        # Signal cards
        cols_inst = st.columns(5)
        for i, comp in enumerate(COMPETITORS):
            ticker = comp["ticker"]
            name = comp["name_cn"]

            with cols_inst[i]:
                comp_sig = inst_signal_df[inst_signal_df["ticker"] == ticker]
                if comp_sig.empty:
                    st.metric(
                        label=f"⚪ {name} ({ticker})",
                        value="暂无",
                        delta="等待采集",
                        delta_color="off",
                    )
                else:
                    latest_sig = comp_sig.iloc[0]
                    score = latest_sig["signal_score"]
                    label = latest_sig["signal_label"]
                    emoji = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(label, "⚪")
                    label_cn = {"bullish": "机构看多", "neutral": "机构中性", "bearish": "机构看空"}.get(label, "?")

                    delta_parts = []
                    if int(latest_sig["new_positions"]) > 0:
                        delta_parts.append(f"+{int(latest_sig['new_positions'])}新")
                    if int(latest_sig["exited_positions"]) > 0:
                        delta_parts.append(f"-{int(latest_sig['exited_positions'])}退")
                    delta_str = " ".join(delta_parts) if delta_parts else "不变"

                    st.metric(
                        label=f"{emoji} {name} ({ticker})",
                        value=f"{label_cn} ({score:+.0f})",
                        delta=delta_str,
                    )

        # Holding details table
        if not inst_holdings_df.empty:
            with st.expander("📋 机构持仓明细（最近一期）"):
                latest_period = inst_holdings_df["report_period"].max()
                latest_holdings = inst_holdings_df[
                    inst_holdings_df["report_period"] == latest_period
                ]

                display_cols = {
                    "ticker": "竞品",
                    "institution_name": "机构",
                    "value_x1000": "市值($K)",
                    "shares": "股数",
                    "share_type": "类型",
                    "report_period": "报告期",
                }
                view = latest_holdings[list(display_cols.keys())].rename(columns=display_cols)
                view["市值($K)"] = view["市值($K)"].apply(
                    lambda x: f"${x:,.0f}K" if pd.notna(x) and x > 0 else "-"
                )
                view["股数"] = view["股数"].apply(
                    lambda x: f"{x:,.0f}" if pd.notna(x) and x > 0 else "-"
                )
                st.dataframe(view, width='stretch', hide_index=True)


# ═══════════════════════════════════════════════════════════
# Block 7: 交叉持股全景（模块 F）
# ═══════════════════════════════════════════════════════════

@st.cache_data(ttl=300)  # 5 分钟缓存，与 load_data() 对齐
def _load_cross_holding_derived():
    """缓存：调用 cross_holding 模块函数获取 QoQ / Buyers / Sellers / Init / Liq / Turnover 数据。

    ⚠️ BUG-9 fix: 复用 cross_holding 模块逻辑，不再在 Dashboard 中独立实现 QoQ。
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(DATABASE_PATH)
    try:
        from cross_holding import (
            compute_qoq_changes,
            find_initiations_liquidations,
            compute_turnover_proxy,
            rank_top_buyers_sellers,
        )
        qoq = compute_qoq_changes(conn)
        buyers = rank_top_buyers_sellers(conn, "buyers", top_n=25)
        sellers = rank_top_buyers_sellers(conn, "sellers", top_n=25)
        init_df, liq_df = find_initiations_liquidations(conn)
        turnover = compute_turnover_proxy(conn)
        return qoq, buyers, sellers, init_df, liq_df, turnover
    finally:
        conn.close()


def render_cross_holding(cross_matrix_df, inst_signal_df):
    """📊 交叉持股全景 — 以 IHS Markit Cross Ownership Report 为模板。

    ⚠️ QoQ / Buyers / Sellers / Init / Liq 数据由 cross_holding 模块函数提供，
    Dashboard 不再重复实现数据计算逻辑（BUG-9 修复）。
    """
    st.subheader("📊 交叉持股全景 (13F)")

    if cross_matrix_df.empty:
        st.info("暂无交叉持股数据。请运行 13F 采集（需等待季末+50天）。")
        return

    # ── 概览卡片 ──
    latest_period = cross_matrix_df["report_period"].max() if "report_period" in cross_matrix_df.columns else "未知"
    total_insts = len(cross_matrix_df)

    st.caption(f"报告期: **{latest_period}**  |  覆盖机构: **{total_insts}** 家（种子池 25 家中的有持仓者）")

    # 从 institutional_signal 汇总统计
    if not inst_signal_df.empty:
        latest_sig_period = inst_signal_df["report_period"].max()
        latest_sigs = inst_signal_df[inst_signal_df["report_period"] == latest_sig_period]
        total_new = int(latest_sigs["new_positions"].sum())
        total_exited = int(latest_sigs["exited_positions"].sum())
        total_inc = int(latest_sigs["increased_positions"].sum())
        total_dec = int(latest_sigs["decreased_positions"].sum())
    else:
        total_new = total_exited = total_inc = total_dec = 0

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("覆盖机构", f"{total_insts} 家")
    with col2:
        st.metric("新建仓", f"+{total_new}")
    with col3:
        st.metric("退出", f"-{total_exited}", delta=f"-{total_exited}", delta_color="inverse")
    with col4:
        st.metric("加仓 >5%", f"{total_inc} 家")
    with col5:
        st.metric("减仓 >5%", f"{total_dec} 家", delta=f"-{total_dec}", delta_color="inverse")

    st.divider()

    # ── Tabs 切换 ──
    tab_names = [
        "📋 Top Holders",
        "📈 QoQ 变动",
        "⚔️ Activists",
        "🟢 Top Buyers",
        "🔴 Top Sellers",
        "🆕 Init / 💀 Liq",
        "📊 Charts",
    ]
    tabs = st.tabs(tab_names)

    competitor_tickers = ["CVNA", "KMX", "AN", "UXIN", "ATHM"]
    MATRIX_VALUE_COLS = [f"{t.lower()}_value_x1000" for t in competitor_tickers]

    # ── 构建 turnover / style lookup ──
    turnover_lookup = {}
    if "turnover_proxy" in cross_matrix_df.columns and "institution_cik" in cross_matrix_df.columns:
        turnover_lookup = dict(zip(cross_matrix_df["institution_cik"], cross_matrix_df["turnover_proxy"]))
    style_map = dict(zip(cross_matrix_df["institution_cik"], cross_matrix_df["style_label"])) if "style_label" in cross_matrix_df.columns and "institution_cik" in cross_matrix_df.columns else {}

    # ── 从 cross_holding 模块获取派生数据（替代原有的 inline QoQ 计算） ──
    qoq_df, buyers_df, sellers_df, init_df, liq_df, turnover_df = _load_cross_holding_derived()

    # 获取两个最近报告期（用于 Tab 2 的 caption）
    qoq_periods = sorted(cross_matrix_df["report_period"].dropna().unique(), reverse=True)[:2] if "report_period" in cross_matrix_df.columns else []
    qoq_has_data = not qoq_df.empty

    # ── Tab 1: Top Holders 热力图 + 表格 ──
    with tabs[0]:
        st.markdown("#### 机构 × 竞品 持仓矩阵")

        if not cross_matrix_df.empty:
            # 热力图
            heatmap_data = cross_matrix_df[MATRIX_VALUE_COLS].copy()
            heatmap_data = heatmap_data.apply(lambda col: col / 1000)  # x1000 → M
            labels = cross_matrix_df["institution_name"].tolist()

            import plotly.graph_objects as go
            fig = go.Figure(data=go.Heatmap(
                z=heatmap_data.values,
                x=competitor_tickers,
                y=labels,
                colorscale="Blues",
                hovertemplate="机构: %{y}<br>竞品: %{x}<br>持仓: $%{z:,.1f}M<extra></extra>",
                colorbar=dict(title="$M"),
            ))
            fig.update_layout(
                height=max(400, 30 * len(labels)),
                margin=dict(l=20, r=20, t=10, b=20),
                xaxis=dict(side="top", title=""),
                yaxis=dict(title="", tickfont=dict(size=11)),
            )
            st.plotly_chart(fig, width='stretch')

            # 增强表格
            with st.expander("📋 数据表格"):
                table_rows = []
                for idx, (_, row) in enumerate(cross_matrix_df.iterrows()):
                    r = {}
                    r["#"] = idx + 1
                    r["机构"] = row.get("institution_name", "-")
                    r["风格"] = row.get("style_label", "-")
                    r["Activism"] = row.get("activism_level", "-") if row.get("activism_level") else "-"
                    r["Turnover"] = row.get("turnover_proxy", "-")
                    total_val = row.get("total_value_x1000", 0) or 0
                    r["总持仓"] = f"${total_val/1000:,.1f}M" if total_val > 0 else "-"
                    change_val = row.get("total_change_x1000", 0) or 0
                    r["Change"] = f"${change_val/1000:+,.1f}M" if change_val != 0 else "-"
                    peer_val = row.get("peer_avg_x1000", 0) or 0
                    r["Peer Avg"] = f"${peer_val/1000:,.1f}M" if peer_val > 0 else "-"
                    for tk in competitor_tickers:
                        col_name = f"{tk.lower()}_value_x1000"
                        val = row.get(col_name, 0) or 0
                        r[tk] = f"${val/1000:,.1f}M" if val > 0 else "-"
                    table_rows.append(r)

                st.dataframe(pd.DataFrame(table_rows), width='stretch', hide_index=True)

            st.caption("<!-- Commentary -->")

    # ── Tab 2: QoQ 变动（数据来自 cross_holding.compute_qoq_changes） ──
    with tabs[1]:
        st.markdown("#### QoQ 持仓变动")
        if not qoq_has_data:
            st.info("需要至少两期 13F 数据才能显示 QoQ 变动。")
        else:
            if len(qoq_periods) >= 2:
                st.caption(f"对比期: {qoq_periods[1]} → {qoq_periods[0]}")
            display = qoq_df.head(30).copy()
            display["Δ ($M)"] = display["delta_value"].apply(lambda x: f"${x/1000:+,.1f}M" if x != 0 else "-")
            display["当前 ($M)"] = display["current_value"].apply(lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-")
            display["上期 ($M)"] = display["previous_value"].apply(lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-")
            display["风格"] = display["institution_cik"].map(style_map).fillna("-")
            display["Turnover"] = display["institution_cik"].map(turnover_lookup).fillna("-")
            st.dataframe(
                display[["institution_name", "ticker", "当前 ($M)", "上期 ($M)", "Δ ($M)", "风格", "Turnover"]].rename(
                    columns={"institution_name": "机构", "ticker": "竞品"}
                ),
                width='stretch', hide_index=True,
            )
            st.caption("<!-- Commentary -->")

    # ── Tab 3: Activists ──
    with tabs[2]:
        st.markdown("#### ⚔️ Top Activists 持仓")
        if not cross_matrix_df.empty and "activism_level" in cross_matrix_df.columns:
            activists_df = cross_matrix_df[
                cross_matrix_df["activism_level"].notna() & (cross_matrix_df["activism_level"] != "")
            ]
            if activists_df.empty:
                st.info("当前无 activist 机构持仓记录。（基于静态种子名单 ~8 家）")
            else:
                activists_df = activists_df.sort_values("total_value_x1000", ascending=False)
                activists_df.insert(0, "Rank", range(1, len(activists_df) + 1))
                display = activists_df[["Rank", "institution_name", "activism_level", "style_label", "turnover_proxy", "total_value_x1000", "peer_avg_x1000"] + MATRIX_VALUE_COLS].copy()
                for col_name in MATRIX_VALUE_COLS + ["total_value_x1000", "peer_avg_x1000"]:
                    if col_name in display.columns:
                        display[col_name] = display[col_name].apply(
                            lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-"
                        )
                st.dataframe(
                    display.rename(columns={
                        "institution_name": "机构", "activism_level": "激进程度",
                        "style_label": "风格", "turnover_proxy": "Turnover",
                        "total_value_x1000": "总持仓", "peer_avg_x1000": "Peer Avg",
                    }),
                    width='stretch', hide_index=True,
                )
            st.caption("<!-- Commentary -->")
        else:
            st.info("暂无 activist 数据。")

    # ── Tab 4: Top Buyers（数据来自 cross_holding.rank_top_buyers_sellers） ──
    with tabs[3]:
        st.markdown("#### Top Peer Buyers（增持排名）")
        if buyers_df.empty:
            st.info("需要至少两期 13F 数据。")
        else:
            # rank_top_buyers_sellers 返回 {ticker}_change 列，需先选取再转换
            change_cols = [f"{tk}_change" for tk in competitor_tickers]
            available_change_cols = [c for c in change_cols if c in buyers_df.columns]
            display = buyers_df[["rank", "institution_name", "style_label", "total_change"] + available_change_cols].copy()
            # 格式化 — 总增持
            display["总增持"] = display["total_change"].apply(lambda x: f"${x/1000:,.1f}M")
            # 各竞品列：从 {tk}_change 转换为 {tk} 展示列
            for tk in competitor_tickers:
                tk_col = f"{tk}_change"
                if tk_col in display.columns:
                    display[tk] = display[tk_col].apply(lambda x: f"${x/1000:+,.1f}M" if x != 0 else "-")
                else:
                    display[tk] = "-"
            # 附加 Turnover
            cik_col = buyers_df["institution_cik"] if "institution_cik" in buyers_df.columns else None
            if cik_col is not None:
                display["Turnover"] = cik_col.map(turnover_lookup).fillna("-")
            else:
                display["Turnover"] = "-"

            st.dataframe(
                display[["rank", "institution_name", "style_label", "Turnover", "总增持"] + competitor_tickers].rename(
                    columns={"rank": "#", "institution_name": "机构", "style_label": "风格"}
                ),
                width='stretch', hide_index=True,
            )
            st.caption("<!-- Commentary -->")

    # ── Tab 5: Top Sellers（数据来自 cross_holding.rank_top_buyers_sellers） ──
    with tabs[4]:
        st.markdown("#### Top Peer Sellers（减持排名）")
        if sellers_df.empty:
            st.info("需要至少两期 13F 数据。")
        else:
            # rank_top_buyers_sellers 返回 {ticker}_change 列，需先选取再转换
            change_cols = [f"{tk}_change" for tk in competitor_tickers]
            available_change_cols = [c for c in change_cols if c in sellers_df.columns]
            display = sellers_df[["rank", "institution_name", "style_label", "total_change"] + available_change_cols].copy()
            # 总减持（sellers 的 total_change 已 abs 化）
            display["总减持"] = display["total_change"].apply(lambda x: f"${x/1000:,.1f}M")
            for tk in competitor_tickers:
                tk_col = f"{tk}_change"
                if tk_col in display.columns:
                    display[tk] = display[tk_col].apply(lambda x: f"${abs(x)/1000:,.1f}M" if x != 0 else "-")
                else:
                    display[tk] = "-"
            cik_col = sellers_df["institution_cik"] if "institution_cik" in sellers_df.columns else None
            if cik_col is not None:
                display["Turnover"] = cik_col.map(turnover_lookup).fillna("-")
            else:
                display["Turnover"] = "-"

            st.dataframe(
                display[["rank", "institution_name", "style_label", "Turnover", "总减持"] + competitor_tickers].rename(
                    columns={"rank": "#", "institution_name": "机构", "style_label": "风格"}
                ),
                width='stretch', hide_index=True,
            )
            st.caption("<!-- Commentary -->")

    # ── Tab 6: Initiations & Liquidations（数据来自 cross_holding.find_initiations_liquidations） ──
    with tabs[5]:
        col_init, col_liq = st.columns(2)

        with col_init:
            st.markdown("##### 🆕 新建仓 (Initiations)")
            if init_df.empty:
                st.info("无新建仓记录。")
            else:
                init_display = init_df.head(10).copy()
                init_display["市值"] = init_display["current_value"].apply(lambda x: f"${x/1000:,.1f}M")
                init_display["Turnover"] = init_display["institution_cik"].map(turnover_lookup).fillna("-")
                st.dataframe(
                    init_display[["institution_name", "ticker", "市值", "style_label", "Turnover"]].rename(
                        columns={"institution_name": "机构", "ticker": "竞品", "style_label": "风格"}
                    ),
                    width='stretch', hide_index=True,
                )

        with col_liq:
            st.markdown("##### 💀 清仓 (Liquidations)")
            if liq_df.empty:
                st.info("无清仓记录。")
            else:
                liq_display = liq_df.head(10).copy()
                liq_display["上期市值"] = liq_display["previous_value"].apply(lambda x: f"${x/1000:,.1f}M")
                liq_display["Turnover"] = liq_display["institution_cik"].map(turnover_lookup).fillna("-")
                st.dataframe(
                    liq_display[["institution_name", "ticker", "上期市值", "style_label", "Turnover"]].rename(
                        columns={"institution_name": "机构", "ticker": "竞品", "style_label": "风格"}
                    ),
                    width='stretch', hide_index=True,
                )

        st.caption("<!-- Commentary -->")

    # ── Tab 7: Charts（本身已调用 cross_holding.compute_capital_flows_by_category） ──
    with tabs[6]:
        flows = _compute_flows_once()
        _render_pie_charts(cross_matrix_df, flows)
        _render_capital_flows_attribution(flows)
        st.caption("<!-- Commentary -->")

    st.divider()

    # ── 免责声明 ──
    with st.expander("⚠️ 免责声明与差距说明"):
        st.markdown("""
        1. **机构覆盖范围**：本看板仅覆盖 Top 25 家种子机构（13F 申报人），不代表完整机构持有人全景。IHS Markit 报告覆盖全市场 ~5,000 家 13F 申报人。

        2. **投资风格标签**：看板中的风格标签（Index / Active / Broker）为基于实体类型的**简化分类**，非 IHS Markit 专业风格标签（Value / Growth / GARP / Aggressive Growth 等 12 类）。详细差距说明见 `prd/reference/gap-analysis-ihs-markit.md`。

        3. **Turnover Proxy**：基于 QoQ 13F 快照的持仓变动率估算（3 档：Low / Medium / High），非精确 portfolio turnover。IHS Markit 基于 12 个月日度交易数据计算 4 档分类。

        4. **数据时滞**：13F 数据滞后约 45 天。看板反映的是季末机构持仓情况，**非实时持仓**。

        5. **激进投资者标注**：仅基于静态种子名单（~8 家已知 activist），不保证覆盖所有有 activist 行为的机构。
        """)

        # 报告下载按钮
        if st.button("📄 生成完整 Markdown 报告"):
            with st.spinner("生成报告中..."):
                import sqlite3 as sqlite3_report
                conn_report = sqlite3_report.connect(DATABASE_PATH)
                from cross_holding import generate_cross_holding_report
                report_text = generate_cross_holding_report(conn_report)
                conn_report.close()
                st.download_button(
                    label="⬇️ 下载 Markdown 报告",
                    data=report_text,
                    file_name=f"cross-ownership-report-{datetime.now().strftime('%Y%m%d')}.md",
                    mime="text/markdown",
                )
                with st.expander("📖 预览报告"):
                    st.markdown(report_text)

    st.divider()


def _compute_flows_once():
    """BUG-15: compute capital flows once, share across pie chart + attribution renderers."""
    import sqlite3
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cross_holding import compute_capital_flows_by_category
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        flows = compute_capital_flows_by_category(conn)
    except Exception as e:
        st.warning(f"饼图数据计算失败：{e}")
        flows = None
    conn.close()
    return flows


def _render_pie_charts(cross_matrix_df, flows):

    pie_data = flows.get("pie_data", {})
    orient_pie = pie_data.get("orientation")
    style_pie = pie_data.get("style")
    turnover_pie = pie_data.get("turnover")

    st.markdown("#### 📊 Investor Orientation Breakdown（IHS P8 简化）")
    st.caption("⚠️ Index = Passive，其余 = Active。非 IHS Markit 精确分类。")

    if orient_pie is not None and not orient_pie.empty:
        import plotly.graph_objects as go
        import plotly.express as px

        col_pie, col_bar = st.columns(2)
        with col_pie:
            fig_pie = px.pie(
                orient_pie, values="count", names="label",
                title="Orientation 占比",
                color="label",
                color_discrete_map={"Passive": "#6366f1", "Active": "#f59e0b"},
                hole=0.3,
            )
            fig_pie.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_pie, width='stretch')
        with col_bar:
            by_orient = flows.get("by_orientation")
            if by_orient is not None and not by_orient.empty:
                df_sorted = by_orient.sort_values("total_flow", ascending=True)
                fig_bar = go.Figure(data=go.Bar(
                    x=df_sorted["total_flow"] / 1000,
                    y=df_sorted["orientation"],
                    orientation="h",
                    marker_color=["#22c55e" if v > 0 else "#ef4444" for v in df_sorted["total_flow"]],
                    hovertemplate="%{y}: $%{x:,.1f}M<extra></extra>",
                ))
                fig_bar.update_layout(
                    title="Capital Flows by Orientation",
                    height=300, margin=dict(l=10, r=10, t=40, b=20),
                    xaxis=dict(title="QoQ 资金流向 ($M)", tickformat=".1f", tickprefix="$"),
                )
                st.plotly_chart(fig_bar, width='stretch')

    st.divider()

    st.markdown("#### 📊 Investor Style Breakdown（IHS P9 简化）")
    st.caption("⚠️ 3 类简化标签 vs IHS Markit 12 类。12 类风格需采购 Morningstar/FactSet 数据。")

    if style_pie is not None and not style_pie.empty:
        col_sp, col_sb = st.columns(2)
        with col_sp:
            fig_sp = px.pie(
                style_pie, values="count", names="label",
                title="Style 占比",
                color="label",
                hole=0.3,
            )
            fig_sp.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_sp, width='stretch')
        with col_sb:
            by_style = flows.get("by_style")
            if by_style is not None and not by_style.empty:
                df_sorted = by_style.sort_values("total_flow", ascending=True)
                fig_sb = go.Figure(data=go.Bar(
                    x=df_sorted["total_flow"] / 1000,
                    y=df_sorted["style_label"],
                    orientation="h",
                    marker_color=["#22c55e" if v > 0 else "#ef4444" for v in df_sorted["total_flow"]],
                    hovertemplate="%{y}: $%{x:,.1f}M<extra></extra>",
                ))
                fig_sb.update_layout(
                    title="Capital Flows by Style",
                    height=300, margin=dict(l=10, r=10, t=40, b=20),
                    xaxis=dict(title="QoQ 资金流向 ($M)", tickformat=".1f", tickprefix="$"),
                )
                st.plotly_chart(fig_sb, width='stretch')

    st.divider()

    st.markdown("#### 📊 Investor Turnover Breakdown（IHS P10 简化）")
    st.caption("⚠️ 3 档估算 vs IHS Markit 4 档（缺 Very Active）。基于 QoQ 13F 快照，非精确 portfolio turnover。")

    if turnover_pie is not None and not turnover_pie.empty:
        col_tp, col_tb = st.columns(2)
        with col_tp:
            # Ensure consistent order: Low → Medium → High
            order_map = {"Low": 0, "Medium": 1, "High": 2}
            turnover_pie_sorted = turnover_pie.copy()
            turnover_pie_sorted["_order"] = turnover_pie_sorted["label"].map(order_map).fillna(99)
            turnover_pie_sorted = turnover_pie_sorted.sort_values("_order")
            fig_tp = px.pie(
                turnover_pie_sorted, values="count", names="label",
                title="Turnover 占比",
                color="label",
                color_discrete_map={"Low": "#22c55e", "Medium": "#f59e0b", "High": "#ef4444"},
                hole=0.3,
            )
            fig_tp.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_tp, width='stretch')
        with col_tb:
            by_turnover = flows.get("by_turnover")
            if by_turnover is not None and not by_turnover.empty:
                df_sorted = by_turnover.sort_values("total_flow", ascending=True)
                fig_tb = go.Figure(data=go.Bar(
                    x=df_sorted["total_flow"] / 1000,
                    y=df_sorted["turnover_label"],
                    orientation="h",
                    marker_color=["#22c55e" if v > 0 else "#ef4444" for v in df_sorted["total_flow"]],
                    hovertemplate="%{y}: $%{x:,.1f}M<extra></extra>",
                ))
                fig_tb.update_layout(
                    title="Capital Flows by Turnover",
                    height=300, margin=dict(l=10, r=10, t=40, b=20),
                    xaxis=dict(title="QoQ 资金流向 ($M)", tickformat=".1f", tickprefix="$"),
                )
                st.plotly_chart(fig_tb, width='stretch')


def _render_capital_flows_attribution(flows):
    """📊 资本流向归因：按风格 / Turnover / Activism 分类的 QoQ 资金流向。"""
    st.markdown("#### 📊 资本流向归因（按分类）")
    st.caption("⚠️ 基于简化分类（非 IHS Markit 12 类专业分类）。数据源：QoQ 13F 持仓变动。")

    if flows is None:
        st.info("暂无资本流向数据。")
        return

    # 4 张柱状图：by_orientation / by_style / by_turnover / by_activism
    col_a, col_b, col_c, col_d = st.columns(4)

    def _plot_bar(df, group_col, title, ax_col):
        if df is None or df.empty:
            with ax_col:
                st.info(f"{title}：暂无数据")
            return
        import plotly.graph_objects as go
        # 横向柱状图，按 total_flow 排序
        df_sorted = df.sort_values("total_flow", ascending=True)
        fig = go.Figure(data=go.Bar(
            x=df_sorted["total_flow"] / 1000,  # x1000 → M
            y=df_sorted[group_col],
            orientation="h",
            marker_color=["#22c55e" if v > 0 else "#ef4444" for v in df_sorted["total_flow"]],
            hovertemplate="%{y}: $%{x:,.1f}M<extra></extra>",
        ))
        fig.update_layout(
            title=title,
            height=250,
            margin=dict(l=10, r=10, t=40, b=20),
            xaxis=dict(title="QoQ 资金流向 ($M)", tickformat=".1f", tickprefix="$"),
            yaxis=dict(title=""),
            showlegend=False,
        )
        with ax_col:
            st.plotly_chart(fig, width='stretch')

    with col_a:
        _plot_bar(flows.get("by_orientation"), "orientation", "按 Active/Passive", col_a)
    with col_b:
        _plot_bar(flows.get("by_style"), "style_label", "按投资风格 (Style)", col_b)
    with col_c:
        _plot_bar(flows.get("by_turnover"), "turnover_label", "按 Turnover", col_c)
    with col_d:
        _plot_bar(flows.get("by_activism"), "activism", "按 Activism", col_d)

    # 详细数据表
    with st.expander("📋 归因明细表"):
        for dim, df, label in [
            ("by_orientation", flows.get("by_orientation"), "Active/Passive"),
            ("by_style", flows.get("by_style"), "Style"),
            ("by_turnover", flows.get("by_turnover"), "Turnover"),
            ("by_activism", flows.get("by_activism"), "Activism"),
        ]:
            if df is None or df.empty:
                continue
            st.markdown(f"**{label} 归因**")
            display = df.copy()
            for col_name in ["CVNA", "KMX", "AN", "UXIN", "ATHM", "total_flow"]:
                if col_name in display.columns:
                    display[col_name] = display[col_name].apply(
                        lambda x: f"${x/1000:+,.1f}M" if col_name == "total_flow" else f"${x/1000:+,.1f}M" if x != 0 else "-"
                    )
            st.dataframe(display, width='stretch', hide_index=True)


# ═══════════════════════════════════════════════════════════
# 主页面
# ═══════════════════════════════════════════════════════════

def main():
    check_db_ready()  # DB 缺失/缺表时直接给中文提示并 stop
    st.title("🏠 竞品情报监控")
    st.caption(f"最后更新: {latest_update(st.session_state.get('events', pd.DataFrame()), st.session_state.get('filings', pd.DataFrame()))}")

    # 加载数据
    with st.spinner("加载数据..."):
        filings, financials, events, ec, insider_txns, form144, insider_sent, inst_holdings, inst_signal, cross_matrix = load_data()
        st.session_state["filings"] = filings
        st.session_state["events"] = events

    # 刷新按钮
    col_refresh, _ = st.columns([1, 9])
    with col_refresh:
        if st.button("🔄 刷新数据"):
            st.cache_data.clear()
            st.rerun()

    st.divider()

    # Block 1: 概览条
    render_overview_bar(filings, events)

    st.divider()

    # Block 2: Filing 时间线
    render_filing_timeline(filings)

    st.divider()

    # Block 3: 财务对比图
    render_financial_chart(financials)

    st.divider()

    # Block 4: 事件预警
    render_event_alerts(events)

    st.divider()

    # Block 5: EC 纪要
    render_ec_notes(ec)

    st.divider()

    # Block 6: 内部人与机构动向（Phase 2）
    render_insider_institutional(
        insider_sent, insider_txns, form144,
        inst_signal, inst_holdings,
    )

    st.divider()

    # Block 7: 交叉持股全景（模块 F）
    render_cross_holding(cross_matrix, inst_signal)


if __name__ == "__main__":
    main()
