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

    # 模块 F: 交叉持股矩阵
    cross_matrix = pd.read_sql_query("""
        SELECT * FROM cross_holding_matrix
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
    st.plotly_chart(fig, use_container_width=True)

    # 数据表格
    with st.expander("查看原始数据"):
        pivot = df.pivot_table(
            index="period", columns="ticker", values="metric_value", aggfunc="first"
        )
        st.dataframe(pivot, use_container_width=True)


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
                st.dataframe(view, use_container_width=True, hide_index=True)

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
                st.dataframe(view, use_container_width=True, hide_index=True)

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
                st.dataframe(view, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════
# Block 7: 交叉持股全景（模块 F）
# ═══════════════════════════════════════════════════════════

def render_cross_holding(cross_matrix_df, inst_holdings_df, inst_signal_df):
    """📊 交叉持股全景 — 以 IHS Markit Cross Ownership Report 为模板。"""
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
    tab_names = ["📋 Top Holders", "📈 QoQ 变动", "🟢 Top Buyers", "🔴 Top Sellers", "🆕 Init / 💀 Liq"]
    tabs = st.tabs(tab_names)

    competitor_tickers = ["CVNA", "KMX", "AN", "UXIN", "ATHM"]

    # ── Tab 1: Top Holders 热力图 + 表格 ──
    with tabs[0]:
        st.markdown("#### 机构 × 竞品 持仓矩阵")

        if not cross_matrix_df.empty:
            # 热力图
            heatmap_data = cross_matrix_df[competitor_tickers].copy()
            # 转为百万美元
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
            st.plotly_chart(fig, use_container_width=True)

            # 表格
            with st.expander("📋 数据表格"):
                display_cols = (
                    ["institution_name", "style_label", "activism_level", "turnover_proxy"]
                    + competitor_tickers
                    + ["total_value_x1000"]
                )
                available = [c for c in display_cols if c in cross_matrix_df.columns]
                view = cross_matrix_df[available].copy()

                # 格式化
                for tk in competitor_tickers:
                    if tk in view.columns:
                        view[tk] = view[tk].apply(
                            lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-"
                        )
                if "total_value_x1000" in view.columns:
                    view["total_value_x1000"] = view["total_value_x1000"].apply(
                        lambda x: f"${x/1000:,.1f}M" if pd.notna(x) and x > 0 else "-"
                    )

                st.dataframe(view, use_container_width=True, hide_index=True)

    # ── Tabs 2-5: 基于 inst_holdings_df 实时计算 ──
    # 计算 QoQ 变动（如果存在两期数据）
    qoq_data = None
    if not inst_holdings_df.empty:
        periods = sorted(inst_holdings_df["report_period"].dropna().unique(), reverse=True)
        if len(periods) >= 2:
            curr_p, prev_p = periods[0], periods[1]
            # 按 institution_cik × ticker 聚合
            curr_agg = inst_holdings_df[inst_holdings_df["report_period"] == curr_p].groupby(
                ["institution_cik", "institution_name", "ticker"]
            )["value_x1000"].sum().reset_index()
            prev_agg = inst_holdings_df[inst_holdings_df["report_period"] == prev_p].groupby(
                ["institution_cik", "ticker"]
            )["value_x1000"].sum().reset_index()
            prev_agg.rename(columns={"value_x1000": "prev_value_x1000"}, inplace=True)

            qoq_merged = curr_agg.merge(
                prev_agg, on=["institution_cik", "ticker"], how="outer"
            )
            qoq_merged["curr_value"] = qoq_merged["value_x1000"].fillna(0)
            qoq_merged["prev_value"] = qoq_merged["prev_value_x1000"].fillna(0)
            qoq_merged["delta"] = qoq_merged["curr_value"] - qoq_merged["prev_value"]
            # 机构名 fill
            if "institution_name" not in qoq_merged.columns:
                qoq_merged["institution_name"] = qoq_merged["institution_cik"]

            qoq_data = qoq_merged[qoq_merged["delta"].abs() > 0].sort_values(
                "delta", key=abs, ascending=False
            )

    # ── Tab 2: QoQ 变动 ──
    with tabs[1]:
        st.markdown("#### QoQ 持仓变动")
        if qoq_data is None or qoq_data.empty:
            st.info("需要至少两期 13F 数据才能显示 QoQ 变动。")
        else:
            st.caption(f"对比期: {periods[1]} → {periods[0]}")
            display = qoq_data.head(30).copy()
            display["Δ ($K)"] = display["delta"].apply(lambda x: f"{x:+,.0f}" if x != 0 else "-")
            display["当前 ($K)"] = display["curr_value"].apply(lambda x: f"{x:,.0f}")
            display["上期 ($K)"] = display["prev_value"].apply(lambda x: f"{x:,.0f}")
            st.dataframe(
                display[["institution_name", "ticker", "当前 ($K)", "上期 ($K)", "Δ ($K)"]].rename(
                    columns={"institution_name": "机构", "ticker": "竞品"}
                ),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 3: Top Buyers ──
    with tabs[2]:
        st.markdown("#### Top Peer Buyers（增持排名）")
        if qoq_data is None or qoq_data.empty:
            st.info("需要至少两期 13F 数据。")
        else:
            buyers = qoq_data[qoq_data["delta"] > 0].groupby(
                ["institution_cik", "institution_name"]
            )["delta"].sum().reset_index().sort_values("delta", ascending=False).head(25)

            # 按竞品展开
            buyers_detail = qoq_data[qoq_data["institution_cik"].isin(buyers["institution_cik"])]
            buyers_pivot = buyers_detail.pivot_table(
                index=["institution_cik", "institution_name"],
                columns="ticker", values="delta", fill_value=0
            ).reset_index()

            buyers_out = buyers.merge(buyers_pivot, on=["institution_cik", "institution_name"], how="left")
            for tk in competitor_tickers:
                if tk not in buyers_out.columns:
                    buyers_out[tk] = 0.0

            buyers_out.insert(0, "Rank", range(1, len(buyers_out) + 1))
            display = buyers_out[["Rank", "institution_name"] + competitor_tickers + ["delta"]].copy()
            for tk in competitor_tickers:
                display[tk] = display[tk].apply(lambda x: f"${x/1000:+,.1f}M" if x != 0 else "-")
            display["delta"] = display["delta"].apply(lambda x: f"${x/1000:,.1f}M")

            st.dataframe(
                display.rename(columns={"institution_name": "机构", "delta": "总增持"}),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 4: Top Sellers ──
    with tabs[3]:
        st.markdown("#### Top Peer Sellers（减持排名）")
        if qoq_data is None or qoq_data.empty:
            st.info("需要至少两期 13F 数据。")
        else:
            sellers = qoq_data[qoq_data["delta"] < 0].groupby(
                ["institution_cik", "institution_name"]
            )["delta"].sum().reset_index().sort_values("delta", ascending=True).head(25)

            sellers_detail = qoq_data[qoq_data["institution_cik"].isin(sellers["institution_cik"])]
            sellers_pivot = sellers_detail.pivot_table(
                index=["institution_cik", "institution_name"],
                columns="ticker", values="delta", fill_value=0
            ).reset_index()

            sellers_out = sellers.merge(sellers_pivot, on=["institution_cik", "institution_name"], how="left")
            for tk in competitor_tickers:
                if tk not in sellers_out.columns:
                    sellers_out[tk] = 0.0

            sellers_out.insert(0, "Rank", range(1, len(sellers_out) + 1))
            display = sellers_out[["Rank", "institution_name"] + competitor_tickers + ["delta"]].copy()
            for tk in competitor_tickers:
                display[tk] = display[tk].apply(lambda x: f"${x/1000:+,.1f}M" if x != 0 else "-")
            display["delta"] = display["delta"].apply(lambda x: f"${abs(x)/1000:,.1f}M")

            st.dataframe(
                display.rename(columns={"institution_name": "机构", "delta": "总减持"}),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 5: Initiations & Liquidations ──
    with tabs[4]:
        col_init, col_liq = st.columns(2)

        with col_init:
            st.markdown("##### 🆕 新建仓 (Initiations)")
            if qoq_data is None or qoq_data.empty:
                st.info("需要至少两期数据。")
            else:
                # Initiation: 上期无、本期有
                inits = qoq_merged[qoq_merged["prev_value_x1000"].isna() | (qoq_merged["prev_value_x1000"] == 0)]
                inits = inits[inits["value_x1000"] > 0].sort_values("value_x1000", ascending=False).head(10)
                if inits.empty:
                    st.info("无新建仓记录。")
                else:
                    display = inits[["institution_name", "ticker", "value_x1000"]].copy()
                    display["市值"] = display["value_x1000"].apply(lambda x: f"${x/1000:,.1f}M")
                    st.dataframe(
                        display[["institution_name", "ticker", "市值"]].rename(
                            columns={"institution_name": "机构", "ticker": "竞品"}
                        ),
                        use_container_width=True, hide_index=True,
                    )

        with col_liq:
            st.markdown("##### 💀 清仓 (Liquidations)")
            if qoq_data is None or qoq_data.empty:
                st.info("需要至少两期数据。")
            else:
                # Liquidation: 上期有、本期无
                liqs = qoq_merged[qoq_merged["value_x1000"].isna() | (qoq_merged["value_x1000"] == 0)]
                liqs = liqs[liqs["prev_value_x1000"] > 0].sort_values("prev_value_x1000", ascending=False).head(10)
                if liqs.empty:
                    st.info("无清仓记录。")
                else:
                    display = liqs[["institution_name", "ticker", "prev_value_x1000"]].copy()
                    display["上期市值"] = display["prev_value_x1000"].apply(lambda x: f"${x/1000:,.1f}M")
                    st.dataframe(
                        display[["institution_name", "ticker", "上期市值"]].rename(
                            columns={"institution_name": "机构", "ticker": "竞品"}
                        ),
                        use_container_width=True, hide_index=True,
                    )

    st.divider()

    # ── 免责声明 ──
    with st.expander("⚠️ 免责声明与差距说明"):
        st.markdown("""
        1. **机构覆盖范围**：本看板仅覆盖 Top 25 家种子机构（13F 申报人），不代表完整机构持有人全景。IHS Markit 报告覆盖全市场 ~5,000 家 13F 申报人。

        2. **投资风格标签**：看板中的风格标签（Index / Active / Broker）为基于实体类型的**简化分类**，非 IHS Markit 专业风格标签（Value / Growth / GARP / Aggressive Growth 等 12 类）。详细差距说明见 `prd/gap-analysis-ihs-markit.md`。

        3. **Turnover Proxy**：基于 QoQ 13F 快照的持仓变动率估算（3 档：Low / Medium / High），非精确 portfolio turnover。IHS Markit 基于 12 个月日度交易数据计算 4 档分类。

        4. **数据时滞**：13F 数据滞后约 45 天。看板反映的是季末机构持仓情况，**非实时持仓**。

        5. **激进投资者标注**：仅基于静态种子名单（~8 家已知 activist），不保证覆盖所有有 activist 行为的机构。
        """)


# ═══════════════════════════════════════════════════════════
# 主页面
# ═══════════════════════════════════════════════════════════

def main():
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
    render_cross_holding(cross_matrix, inst_holdings, inst_signal)


if __name__ == "__main__":
    main()
