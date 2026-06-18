"""
竞品情报监控系统 — 交叉持股分析引擎（模块 F）

以 IHS Markit Cross Ownership Report 为交付模板，对已采集的 13F 数据
进行多维分析：

1. build_cross_holding_matrix()    — 机构 × 竞品 交叉持股矩阵
2. compute_qoq_changes()           — QoQ 持仓变动
3. rank_top_buyers_sellers()       — Top Buyers / Sellers 排名
4. find_initiations_liquidations() — 新建仓 / 清仓识别
5. compute_turnover_proxy()        — 持仓换手率近似估算
6. generate_cross_holding_report() — Markdown 格式报告

与 IHS Markit 差距说明见 /Users/liuming/sec/prd/reference/gap-analysis-ihs-markit.md
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from config import (
    COMPETITORS,
    DATABASE_PATH,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

COMPETITOR_TICKERS = [c["ticker"] for c in COMPETITORS]

# Turnover proxy 分档阈值
TURNOVER_THRESHOLDS = [
    (0.20, "Low"),
    (0.50, "Medium"),
]
# > 0.50 → High


def _get_db(db_path=None):
    path = db_path or DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ═══════════════════════════════════════════════════════════
# 1. 交叉持股矩阵
# ═══════════════════════════════════════════════════════════

def build_cross_holding_matrix(conn, report_period=None):
    """
    从 institutional_holdings 构建机构 × 竞品的交叉持股矩阵。

    Args:
        conn: SQLite 连接
        report_period: 指定报告期（"2026-03-31"），None = 最新一期

    Returns:
        DataFrame，列 = [institution_name, institution_cik,
                         CVNA, KMX, AN, UXIN, ATHM,
                         total_value_x1000, style_label, activism_level,
                         turnover_proxy]
    """
    # 获取最新报告期
    if report_period is None:
        row = conn.execute("""
            SELECT report_period FROM institutional_holdings
            WHERE report_period != ''
            ORDER BY report_period DESC LIMIT 1
        """).fetchone()
        if not row:
            logger.warning("No 13F data available for cross-holding matrix.")
            return pd.DataFrame()
        report_period = row[0]

    logger.info("Building cross-holding matrix for %s", report_period)

    # 读取该报告期的全部持仓，JOIN 机构风格标签
    df = pd.read_sql_query("""
        SELECT
            ih.institution_name,
            ih.institution_cik,
            ih.ticker,
            ih.value_x1000,
            COALESCE(ist.style_label, 'Unclassified') AS style_label,
            ist.activism_level
        FROM institutional_holdings ih
        LEFT JOIN institution_styles ist ON ih.institution_cik = ist.institution_cik
        WHERE ih.report_period = ?
    """, conn, params=(report_period,))

    if df.empty:
        logger.warning("No holdings found for period %s", report_period)
        return pd.DataFrame()

    # 按机构 + ticker 聚合（同一机构可能在多条记录中持有同一 ticker）
    df = df.groupby(
        ["institution_name", "institution_cik", "ticker"],
        dropna=False,
    )["value_x1000"].sum().reset_index()

    # 透视：机构 → 行，竞品 ticker → 列
    matrix = df.pivot_table(
        index=["institution_name", "institution_cik"],
        columns="ticker",
        values="value_x1000",
        fill_value=0,
    ).reset_index()

    # 附加风格标签（从 institution_styles 表直接获取，避免 NaN 导致 pivot 错位）
    styles = pd.read_sql_query("""
        SELECT institution_cik, style_label, activism_level
        FROM institution_styles
    """, conn)
    matrix = matrix.merge(styles, on="institution_cik", how="left")
    matrix["style_label"] = matrix["style_label"].fillna("Unclassified")
    matrix["activism_level"] = matrix["activism_level"].fillna("")

    # 确保所有 5 家竞品列都存在
    for tk in COMPETITOR_TICKERS:
        if tk not in matrix.columns:
            matrix[tk] = 0.0

    # 列重排：机构名 + 5 家竞品
    col_order = (
        ["institution_name", "institution_cik", "style_label", "activism_level"]
        + COMPETITOR_TICKERS
    )
    matrix = matrix[col_order]

    # 计算总持仓
    matrix["total_value_x1000"] = matrix[COMPETITOR_TICKERS].sum(axis=1)

    # 计算 Peer Average（同业组 5 家的算术均值）
    # 注：与 IHS Markit "Peer Average" 口径不同——IHS 是在所有机构层面对每只竞品求均值，
    # 我们是在机构层面对该机构实际持有的 5 只竞品求均值。
    matrix["peer_avg_x1000"] = matrix[COMPETITOR_TICKERS].mean(axis=1).round(0)

    # 计算 QoQ Change（上期 vs 本期总持仓变动），用于在 P2 表格中同步展示
    total_change_map = {}
    periods = conn.execute("""
        SELECT DISTINCT report_period FROM institutional_holdings
        WHERE report_period != ''
        ORDER BY report_period DESC LIMIT 2
    """).fetchall()
    if len(periods) >= 2:
        curr_p, prev_p = periods[0][0], periods[1][0]
        curr_totals = pd.read_sql_query("""
            SELECT institution_cik, SUM(value_x1000) AS total_curr
            FROM institutional_holdings WHERE report_period = ?
            GROUP BY institution_cik
        """, conn, params=(curr_p,))
        prev_totals = pd.read_sql_query("""
            SELECT institution_cik, SUM(value_x1000) AS total_prev
            FROM institutional_holdings WHERE report_period = ?
            GROUP BY institution_cik
        """, conn, params=(prev_p,))
        change_df = curr_totals.merge(prev_totals, on="institution_cik", how="outer")
        # BUG-1/B2 fix: compute real change, handling new and exited institutions
        change_df["total_change_x1000"] = change_df.apply(
            lambda r: (r["total_curr"] if pd.notna(r["total_curr"]) else 0)
                    - (r["total_prev"] if pd.notna(r["total_prev"]) else 0),
            axis=1,
        )
        total_change_map = {r["institution_cik"]: r["total_change_x1000"]
                           for _, r in change_df.iterrows()
                           if pd.notna(r["total_change_x1000"])}

    matrix["total_change_x1000"] = matrix["institution_cik"].map(total_change_map).fillna(0)

    # 按总持仓降序排列
    matrix = matrix.sort_values("total_value_x1000", ascending=False).reset_index(drop=True)

    # 写入缓存表
    _write_matrix_to_db(conn, matrix, report_period)

    return matrix


def _write_matrix_to_db(conn, matrix, report_period):
    """将交叉持股矩阵写入 cross_holding_matrix 表。"""
    # 幂等建表（允许单独运行 cross_holding.py 而不依赖 institutional_tracker.py）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cross_holding_matrix (
            id                  INTEGER PRIMARY KEY,
            report_period       TEXT,
            institution_name    TEXT,
            institution_cik     TEXT,
            cvna_value_x1000    REAL DEFAULT 0,
            kmx_value_x1000     REAL DEFAULT 0,
            an_value_x1000      REAL DEFAULT 0,
            uxin_value_x1000    REAL DEFAULT 0,
            athm_value_x1000    REAL DEFAULT 0,
            total_value_x1000   REAL DEFAULT 0,
            total_change_x1000  REAL DEFAULT 0,
            peer_avg_x1000      REAL DEFAULT 0,
            style_label         TEXT,
            activism_level      TEXT,
            turnover_proxy      TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(institution_cik, report_period)
        );
    """)
    import math
    for _, row in matrix.iterrows():
        change_val = row.get("total_change_x1000")
        if change_val is None or (isinstance(change_val, float) and math.isnan(change_val)):
            change_val = None
        conn.execute("""
            INSERT OR REPLACE INTO cross_holding_matrix
                (report_period, institution_name, institution_cik,
                 cvna_value_x1000, kmx_value_x1000, an_value_x1000,
                 uxin_value_x1000, athm_value_x1000,
                 total_value_x1000, total_change_x1000, peer_avg_x1000,
                 style_label, activism_level, turnover_proxy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report_period,
            row["institution_name"],
            row["institution_cik"],
            float(row.get("CVNA", 0)),
            float(row.get("KMX", 0)),
            float(row.get("AN", 0)),
            float(row.get("UXIN", 0)),
            float(row.get("ATHM", 0)),
            float(row["total_value_x1000"]),
            change_val,
            float(row.get("peer_avg_x1000", 0)),
            row.get("style_label"),
            row.get("activism_level"),
            row.get("turnover_proxy"),
        ))
    conn.commit()
    logger.info("  Wrote %d rows to cross_holding_matrix", len(matrix))


# ═══════════════════════════════════════════════════════════
# 2. QoQ 持仓变动
# ═══════════════════════════════════════════════════════════

def compute_qoq_changes(conn):
    """
    对比最近两期 13F，计算每个机构在每只竞品的 QoQ 持仓变动。

    Returns:
        DataFrame，列 = [institution_name, institution_cik,
                         ticker, current_value, previous_value,
                         delta_value, delta_pct, style_label]
        按 |delta_value| 降序排列。
    """
    periods = conn.execute("""
        SELECT DISTINCT report_period FROM institutional_holdings
        WHERE report_period != ''
        ORDER BY report_period DESC LIMIT 2
    """).fetchall()

    if len(periods) < 2:
        logger.info("Need at least 2 report periods for QoQ comparison (have %d)", len(periods))
        return pd.DataFrame()

    current_period = periods[0][0]
    previous_period = periods[1][0]
    logger.info("Computing QoQ changes: %s vs %s", previous_period, current_period)

    # 拉取两期数据
    curr_df = pd.read_sql_query("""
        SELECT institution_name, institution_cik, ticker,
               SUM(value_x1000) AS value_x1000
        FROM institutional_holdings
        WHERE report_period = ?
        GROUP BY institution_cik, ticker, institution_name
    """, conn, params=(current_period,))

    prev_df = pd.read_sql_query("""
        SELECT institution_name, institution_cik, ticker,
               SUM(value_x1000) AS value_x1000
        FROM institutional_holdings
        WHERE report_period = ?
        GROUP BY institution_cik, ticker, institution_name
    """, conn, params=(previous_period,))

    # Merge 两期
    merged = pd.merge(
        curr_df,
        prev_df,
        on=["institution_cik", "ticker"],
        how="outer",
        suffixes=("_curr", "_prev"),
    )

    # 填充缺失的机构名
    merged["institution_name"] = (
        merged["institution_name_curr"].fillna(merged["institution_name_prev"])
    )

    # BUG-4 fix: only filter out rows where BOTH periods have no data (pure noise)
    # Keep prev-only institutions (fully exited → valid sell) and curr-only (new → valid buy)
    merged = merged[~(
        (merged["value_x1000_curr"].isna() | (merged["value_x1000_curr"] == 0)) &
        (merged["value_x1000_prev"].isna() | (merged["value_x1000_prev"] == 0))
    )]

    merged["value_curr"] = merged["value_x1000_curr"].fillna(0)
    merged["value_prev"] = merged["value_x1000_prev"].fillna(0)
    merged["delta_value"] = merged["value_curr"] - merged["value_prev"]

    # 计算变动百分比（避免除零）
    merged["delta_pct"] = merged.apply(
        lambda r: (r["delta_value"] / r["value_prev"] * 100)
        if r["value_prev"] > 0 else (100.0 if r["delta_value"] > 0 else -100.0),
        axis=1,
    )

    # 附加风格标签
    styles = _load_styles_lookup(conn)
    merged["style_label"] = merged["institution_cik"].map(styles).fillna("Unclassified")

    # 精简列
    result = merged[[
        "institution_name", "institution_cik", "ticker",
        "value_curr", "value_prev", "delta_value", "delta_pct",
        "style_label",
    ]].rename(columns={
        "value_curr": "current_value",
        "value_prev": "previous_value",
    })

    result = result.sort_values("delta_value", key=abs, ascending=False).reset_index(drop=True)
    return result


def _load_styles_lookup(conn):
    """加载机构 CIK → style_label 的字典。"""
    rows = conn.execute(
        "SELECT institution_cik, style_label FROM institution_styles"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ═══════════════════════════════════════════════════════════
# 3. Top Buyers / Sellers 排名
# ═══════════════════════════════════════════════════════════

def rank_top_buyers_sellers(conn, direction="buyers", top_n=25):
    """
    跨竞品汇总 QoQ 变动，按总变动金额排名。

    Args:
        conn: SQLite 连接
        direction: "buyers"（增持）或 "sellers"（减持）
        top_n: 返回前 N 名

    Returns:
        DataFrame，每行 = 一个机构，列 = [rank, institution_name,
        institution_cik, total_change, CVNA_change, KMX_change,
        AN_change, UXIN_change, ATHM_change, style_label]
    """
    qoq = compute_qoq_changes(conn)
    if qoq.empty:
        return pd.DataFrame()

    # 按机构汇总变动
    agg = qoq.groupby(["institution_name", "institution_cik", "style_label"]).agg(
        total_change=("delta_value", "sum"),
    ).reset_index()

    if direction == "buyers":
        agg = agg[agg["total_change"] > 0].sort_values("total_change", ascending=False)
    elif direction == "sellers":
        agg = agg[agg["total_change"] < 0].sort_values("total_change", ascending=True)
        # 将负数转为正数显示（更直观）
        agg["total_change"] = agg["total_change"].abs()
    else:
        raise ValueError("direction must be 'buyers' or 'sellers'")

    agg = agg.head(top_n).reset_index(drop=True)

    # 添加每只竞品的变动明细
    for tk in COMPETITOR_TICKERS:
        tk_col = f"{tk}_change"
        tk_data = qoq[qoq["ticker"] == tk].groupby("institution_cik")["delta_value"].sum()
        agg[tk_col] = agg["institution_cik"].map(tk_data).fillna(0)

    agg.insert(0, "rank", range(1, len(agg) + 1))

    logger.info("  Top %d %s ranked", len(agg), direction)
    return agg


# ═══════════════════════════════════════════════════════════
# 4. Initiations / Liquidations
# ═══════════════════════════════════════════════════════════

def find_initiations_liquidations(conn):
    """
    识别新建仓（Initiation）和完全清仓（Liquidation）。

    Initiation = 上期无某竞品持仓，本期有
    Liquidation = 上期有某竞品持仓，本期无

    Returns:
        (initiations_df, liquidations_df)
        各 DataFrame 列 = [institution_name, institution_cik, ticker,
                          current_value, style_label]
    """
    periods = conn.execute("""
        SELECT DISTINCT report_period FROM institutional_holdings
        WHERE report_period != ''
        ORDER BY report_period DESC LIMIT 2
    """).fetchall()

    if len(periods) < 2:
        logger.info("Need at least 2 report periods (have %d)", len(periods))
        return pd.DataFrame(), pd.DataFrame()

    current_period = periods[0][0]
    previous_period = periods[1][0]

    # 获取每期的 (institution_cik, ticker) 集合
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

    curr_pairs = _get_pairs(current_period)
    prev_pairs = _get_pairs(previous_period)

    # Initiation: 在 curr 但不在 prev
    initiations = []
    for (cik, ticker), (name, val, style) in curr_pairs.items():
        if (cik, ticker) not in prev_pairs:
            initiations.append({
                "institution_name": name,
                "institution_cik": cik,
                "ticker": ticker,
                "current_value": val,
                "style_label": style,
            })

    # Liquidation: 在 prev 但不在 curr
    liquidations = []
    for (cik, ticker), (name, val, style) in prev_pairs.items():
        if (cik, ticker) not in curr_pairs:
            liquidations.append({
                "institution_name": name,
                "institution_cik": cik,
                "ticker": ticker,
                "previous_value": val,
                "style_label": style,
            })

    init_df = pd.DataFrame(initiations).sort_values(
        "current_value", ascending=False
    ).reset_index(drop=True) if initiations else pd.DataFrame()

    liq_df = pd.DataFrame(liquidations).sort_values(
        "previous_value", ascending=False
    ).reset_index(drop=True) if liquidations else pd.DataFrame()

    logger.info(
        "  Found %d initiations, %d liquidations",
        len(initiations), len(liquidations),
    )
    return init_df, liq_df


# ═══════════════════════════════════════════════════════════
# 4b. Top Activists 排名（对应 IHS Markit 报告第 4 页）
# ═══════════════════════════════════════════════════════════

def rank_top_activists(conn):
    """
    从已采集的持仓中筛选 activist 机构的持仓，生成 IHS P4 格式的排名表。

    Returns:
        DataFrame，列 = [rank, institution_name, institution_cik,
                         activism_level, total_value_x1000] +
                        [CVNA, KMX, AN, UXIN, ATHM]
    """
    matrix = build_cross_holding_matrix(conn)
    if matrix.empty:
        return pd.DataFrame()

    # 筛选 activism_level 不为空的机构
    activists = matrix[
        (matrix["activism_level"].notna()) & (matrix["activism_level"] != "")
    ].copy()

    if activists.empty:
        logger.info("No activist institutions found in current holdings.")
        return pd.DataFrame()

    activists = activists.sort_values("total_value_x1000", ascending=False).reset_index(drop=True)
    activists.insert(0, "rank", range(1, len(activists) + 1))

    logger.info("  Found %d activist institutions with holdings", len(activists))
    return activists


# ═══════════════════════════════════════════════════════════
# 5. Turnover Proxy 估算
# ═══════════════════════════════════════════════════════════

def compute_turnover_proxy(conn):
    """
    基于 QoQ 13F 快照计算持仓变动率（Churn Proxy）。

    公式：Churn Proxy = Σ|Δvalue_i| / Σ avg(value_i)
          （仅针对竞品 peer group）

    分档：
      < 20%  → Low
      20-50% → Medium
      > 50%  → High

    Returns:
        DataFrame，列 = [institution_name, institution_cik,
                         churn_proxy, churn_label, total_abs_delta,
                         total_avg_value]
    """
    qoq = compute_qoq_changes(conn)
    if qoq.empty:
        return pd.DataFrame()

    results = []
    for cik in qoq["institution_cik"].unique():
        inst_data = qoq[qoq["institution_cik"] == cik]
        # BUG-6 fix: skip prev-only institutions (no current holdings → not meaningful)
        if (inst_data["current_value"] == 0).all():
            continue
        name = inst_data["institution_name"].iloc[0]
        style = inst_data["style_label"].iloc[0]

        total_abs_delta = inst_data["delta_value"].abs().sum()
        total_avg_value = (inst_data["current_value"].sum() + inst_data["previous_value"].sum()) / 2

        if total_avg_value > 0:
            churn = total_abs_delta / total_avg_value
        else:
            churn = 0

        # 分档
        if churn < TURNOVER_THRESHOLDS[0][0]:
            label = "Low"
        elif churn < TURNOVER_THRESHOLDS[1][0]:
            label = "Medium"
        else:
            label = "High"

        results.append({
            "institution_name": name,
            "institution_cik": cik,
            "churn_proxy": round(churn * 100, 1),
            "churn_label": label,
            "style_label": style,
            "total_abs_delta": total_abs_delta,
            "total_avg_value": total_avg_value,
        })

    df = pd.DataFrame(results).sort_values("churn_proxy", ascending=False).reset_index(drop=True)
    logger.info("  Computed turnover proxy for %d institutions", len(df))

    # 回写到 cross_holding_matrix
    _update_turnover_in_matrix(conn, df)

    return df


def _update_turnover_in_matrix(conn, turnover_df):
    """更新 cross_holding_matrix 表中的 turnover_proxy 列。"""
    latest_period = conn.execute("""
        SELECT report_period FROM cross_holding_matrix
        ORDER BY report_period DESC LIMIT 1
    """).fetchone()

    if not latest_period:
        return

    period = latest_period[0]
    for _, row in turnover_df.iterrows():
        conn.execute("""
            UPDATE cross_holding_matrix
            SET turnover_proxy = ?
            WHERE institution_cik = ? AND report_period = ?
        """, (row["churn_label"], row["institution_cik"], period))
    conn.commit()


# ═══════════════════════════════════════════════════════════
# 5b. 资本流向归因（按风格 / Turnover / Activism 分类）
# ═══════════════════════════════════════════════════════════

def compute_capital_flows_by_category(conn):
    """
    对 QoQ 持仓变动按不同分类维度做归因聚合。

    对应 IHS Markit 报告第 8-10 页的 "Capital Flows by Investor
    Orientation/Style/Turnover" 柱状图（简化版）。

    Returns:
        dict:
          - "by_style":      DataFrame[category, total_flow, ticker_CVNA, ...]
          - "by_orientation": DataFrame[Active/Passive]
          - "by_turnover":   DataFrame
          - "by_activism":   DataFrame
          - "pie_data":      {style, orientation, turnover} 各分类的占比数据

    注：⚠️ 依赖简化分类（非 IHS 等价）。
    """
    qoq = compute_qoq_changes(conn)
    if qoq.empty:
        return {
            "by_style": pd.DataFrame(), "by_orientation": pd.DataFrame(),
            "by_turnover": pd.DataFrame(), "by_activism": pd.DataFrame(),
            "pie_data": {},
        }

    # 附加 turnover + activism 标签
    turnover_df = compute_turnover_proxy(conn)
    turnover_map = dict(zip(
        turnover_df["institution_cik"],
        turnover_df["churn_label"],
    )) if not turnover_df.empty else {}

    styles = pd.read_sql_query(
        "SELECT institution_cik, style_label, activism_level FROM institution_styles",
        conn,
    )
    activism_map = dict(zip(
        styles["institution_cik"],
        styles["activism_level"].fillna("none"),
    ))

    qoq["turnover_label"] = qoq["institution_cik"].map(turnover_map).fillna("Unknown")
    qoq["activism"] = qoq["institution_cik"].map(activism_map).fillna("none")

    # 0) Orientation: Index → Passive, 其余 → Active
    style_map = dict(zip(styles["institution_cik"], styles["style_label"].fillna("Unclassified")))
    qoq["orientation"] = qoq["institution_cik"].map(style_map).apply(
        lambda x: "Passive" if x == "Index" else "Active"
    )

    # 1) 按 style_label 归因
    by_style = _aggregate_flows(qoq, "style_label")

    # 2) 按 orientation 归因 (Active / Passive 二分)
    by_orientation = _aggregate_flows(qoq, "orientation")

    # 3) 按 turnover 归因
    by_turnover = _aggregate_flows(qoq, "turnover_label")

    # 4) 按 activism 归因
    by_activism = _aggregate_flows(qoq, "activism")

    logger.info(
        "  Capital flows by category: %d style, %d orientation, %d turnover, %d activism buckets",
        len(by_style), len(by_orientation), len(by_turnover), len(by_activism),
    )

    # 构建饼图数据（各分类的机构数占比 + 资金流向占比）
    pie_data = _compute_pie_data(conn, qoq)

    return {
        "by_style": by_style,
        "by_orientation": by_orientation,
        "by_turnover": by_turnover,
        "by_activism": by_activism,
        "pie_data": pie_data,
    }


def _compute_pie_data(conn, qoq_df):
    """
    计算 IHS 报告第 8-10 页饼图所需的数据：
    - Orientation: Active vs Passive 机构数占比
    - Style: 3 类风格占比
    - Turnover: Low/Medium/High 占比

    Returns:
        dict: {"orientation": DataFrame, "style": DataFrame, "turnover": DataFrame}
              每个 DataFrame 有 label, count, pct 列
    """
    # 从 cross_holding_matrix 获取所有有风格标签的机构
    matrix = pd.read_sql_query("""
        SELECT institution_name, institution_cik, style_label, turnover_proxy, activism_level
        FROM cross_holding_matrix
        ORDER BY report_period DESC
    """, conn)

    if matrix.empty:
        return {}

    # 去重（同一机构多期只取最新）
    matrix = matrix.drop_duplicates(subset="institution_cik", keep="first")

    pie_data = {}

    # Orientation pie: Index → Passive, 其余 → Active
    matrix["orientation"] = matrix["style_label"].apply(
        lambda x: "Passive" if x == "Index" else "Active"
    )
    orient_counts = matrix["orientation"].value_counts().reset_index()
    orient_counts.columns = ["label", "count"]
    orient_counts["pct"] = (orient_counts["count"] / orient_counts["count"].sum() * 100).round(1)
    pie_data["orientation"] = orient_counts

    # Style pie
    style_counts = matrix["style_label"].value_counts().reset_index()
    style_counts.columns = ["label", "count"]
    style_counts["pct"] = (style_counts["count"] / style_counts["count"].sum() * 100).round(1)
    pie_data["style"] = style_counts

    # Turnover pie
    turnover_counts = matrix["turnover_proxy"].value_counts().reset_index()
    turnover_counts.columns = ["label", "count"]
    turnover_counts["pct"] = (turnover_counts["count"] / turnover_counts["count"].sum() * 100).round(1)
    pie_data["turnover"] = turnover_counts

    return pie_data


def _aggregate_flows(qoq_df, group_col):
    """按指定列分组聚合 QoQ 资金流向，并按竞品展开。"""
    agg = qoq_df.groupby(group_col).agg(
        total_flow=("delta_value", "sum"),
    ).reset_index()

    # 按竞品展开
    pivot = qoq_df.pivot_table(
        index=group_col,
        columns="ticker",
        values="delta_value",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()

    for tk in COMPETITOR_TICKERS:
        if tk not in pivot.columns:
            pivot[tk] = 0.0

    result = pivot.merge(agg, on=group_col, how="left")
    return result.sort_values("total_flow", ascending=False).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════
# 6. 报告模板引擎
# ═══════════════════════════════════════════════════════════

# Jinja2 模板目录
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)


def _fmt_m(value_x1000):
    """将 x1000 美元值格式化为 $M 字符串，0/- → '-'。"""
    if value_x1000 is None:
        return "-"
    v = float(value_x1000)
    if v == 0:
        return "-"
    return f"${v/1000:,.1f}"


def _fmt_delta_m(value_x1000):
    """将 x1000 美元变动值格式化为带正负号的 $M 字符串。"""
    if value_x1000 is None:
        return "-"
    v = float(value_x1000)
    if v == 0:
        return "-"
    return f"${v/1000:+,.1f}"


def _df_rows(df, columns, formatters=None):
    """将 DataFrame 转为 list[dict]（每行一个 dict），对指定列应用 formatters。"""
    rows = []
    for _, row in df.iterrows():
        r = {}
        for col in columns:
            val = row.get(col)
            if formatters and col in formatters:
                val = formatters[col](val)
            else:
                val = val if val is not None else ""
            r[col] = val
        rows.append(r)
    return rows


def _lookup(df, key_col, val_col):
    """从 DataFrame 构建 {key: value} 字典（快速 lookup）。"""
    if df is None or df.empty:
        return {}
    return dict(zip(df[key_col], df[val_col]))


def generate_cross_holding_report(conn):
    """
    生成 Markdown 格式的交叉持股分析报告（Jinja2 模板渲染）。

    Returns:
        str: Markdown 报告内容
    """
    # 获取数据
    matrix = build_cross_holding_matrix(conn)
    if matrix.empty:
        return "# 交叉持股分析报告\n\n**暂无 13F 数据。** 请先运行 13F 采集。\n"

    qoq = compute_qoq_changes(conn)
    buyers = rank_top_buyers_sellers(conn, "buyers", top_n=25)
    sellers = rank_top_buyers_sellers(conn, "sellers", top_n=25)
    init_df, liq_df = find_initiations_liquidations(conn)
    turnover = compute_turnover_proxy(conn)
    turnover_lookup = _lookup(turnover, "institution_cik", "churn_label")
    activists = rank_top_activists(conn)
    flows = compute_capital_flows_by_category(conn)

    # 报告期
    report_period = conn.execute("""
        SELECT report_period FROM institutional_holdings
        WHERE report_period != ''
        ORDER BY report_period DESC LIMIT 1
    """).fetchone()
    period_str = report_period[0] if report_period else "未知"

    total_institutions = len(matrix)
    activist_count = len(matrix[(matrix['activism_level'].notna()) & (matrix['activism_level'] != '')])

    # ── 构建模板数据 ──

    competitors = COMPETITOR_TICKERS  # ["CVNA", "KMX", "AN", "UXIN", "ATHM"]

    # §1: Top Holders Positions
    top_holders = []
    for i, (_, row) in enumerate(matrix.iterrows()):
        cik = row.get("institution_cik", "")
        top_holders.append({
            "rank": i + 1,
            "name": row["institution_name"],
            "style": row.get("style_label", "-"),
            "turnover": turnover_lookup.get(cik, "-"),
            "total": _fmt_m(row.get("total_value_x1000")),
            "change": _fmt_delta_m(row.get("total_change_x1000")),
            "peer_avg": _fmt_m(row.get("peer_avg_x1000")),
            **{tk: _fmt_m(row.get(tk)) for tk in competitors},
        })

    # §2: QoQ Changes
    qoq_changes = []
    if not qoq.empty:
        for i, (_, row) in enumerate(qoq.head(25).iterrows()):
            qoq_changes.append({
                "rank": i + 1,
                "name": row["institution_name"],
                "ticker": row["ticker"],
                "current": _fmt_m(row.get("current_value")),
                "previous": _fmt_m(row.get("previous_value")),
                "delta": _fmt_delta_m(row.get("delta_value")),
                "delta_pct": f"{row.get('delta_pct', 0):+.1f}%",
                "style": row.get("style_label", "-"),
            })

    # §3: Activists
    activists_rows = []
    if not activists.empty:
        for _, row in activists.iterrows():
            activists_rows.append({
                "rank": int(row.get("rank", 0)),
                "name": row["institution_name"],
                "activism": row.get("activism_level", "-"),
                "total": _fmt_m(row.get("total_value_x1000")),
                **{tk: _fmt_m(row.get(tk)) for tk in competitors},
            })

    # §4: Top Buyers
    buyers_rows = []
    if not buyers.empty:
        for _, row in buyers.iterrows():
            cik = row.get("institution_cik", "")
            buyers_rows.append({
                "rank": int(row.get("rank", 0)),
                "name": row["institution_name"],
                "style": row.get("style_label", "-"),
                "turnover": turnover_lookup.get(cik, "-"),
                "total": _fmt_m(row.get("total_change")),
                **{tk: _fmt_m(row.get(f"{tk}_change")) for tk in competitors},
            })

    # §5: Top Sellers
    sellers_rows = []
    if not sellers.empty:
        for _, row in sellers.iterrows():
            cik = row.get("institution_cik", "")
            # seller total_change is already abs()'d in rank_top_buyers_sellers
            sellers_rows.append({
                "rank": int(row.get("rank", 0)),
                "name": row["institution_name"],
                "style": row.get("style_label", "-"),
                "turnover": turnover_lookup.get(cik, "-"),
                "total": _fmt_m(row.get("total_change")),
                **{tk: _fmt_m(abs(row.get(f"{tk}_change", 0))) for tk in competitors},
            })

    # §6-7: Initiations / Liquidations
    initiations = []
    if not init_df.empty:
        for i, (_, row) in enumerate(init_df.head(10).iterrows()):
            cik = row.get("institution_cik", "")
            initiations.append({
                "rank": i + 1,
                "name": row["institution_name"],
                "ticker": row["ticker"],
                "value": _fmt_m(row.get("current_value")),
                "style": row.get("style_label", "-"),
                "turnover": turnover_lookup.get(cik, "-"),
            })

    liquidations = []
    if not liq_df.empty:
        for i, (_, row) in enumerate(liq_df.head(10).iterrows()):
            cik = row.get("institution_cik", "")
            liquidations.append({
                "rank": i + 1,
                "name": row["institution_name"],
                "ticker": row["ticker"],
                "value": _fmt_m(row.get("previous_value")),
                "style": row.get("style_label", "-"),
                "turnover": turnover_lookup.get(cik, "-"),
            })

    # §8: Investor Style Comparison (pie data)
    pie_data = flows.get("pie_data", {})
    orient_pie = pie_data.get("orientation", pd.DataFrame())
    turn_pie = pie_data.get("turnover", pd.DataFrame())

    orientation = []
    if not orient_pie.empty:
        for _, r in orient_pie.iterrows():
            orientation.append({"label": str(r["label"]), "count": int(r["count"]), "pct": str(r["pct"])})

    turnover_pie_rows = []
    if not turn_pie.empty:
        for _, r in turn_pie.iterrows():
            turnover_pie_rows.append({"label": str(r["label"]), "count": int(r["count"]), "pct": str(r["pct"])})

    # §9: Turnover Proxy
    turnover_rows = []
    if not turnover.empty:
        for _, row in turnover.iterrows():
            turnover_rows.append({
                "name": row["institution_name"],
                "churn": f"{row['churn_proxy']:.1f}%",
                "label": row["churn_label"],
                "style": row.get("style_label", "-"),
            })

    # ── 渲染 ──
    template = _jinja_env.get_template("cross_holding_report.md.j2")
    return template.render(
        report_period=period_str,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_institutions=total_institutions,
        activist_count=activist_count,
        top_holders=top_holders,
        qoq_changes=qoq_changes,
        activists=activists_rows,
        buyers=buyers_rows,
        sellers=sellers_rows,
        initiations=initiations,
        liquidations=liquidations,
        orientation=orientation,
        turnover=turnover_pie_rows,
        turnover_list=turnover_rows,
    )


# ═══════════════════════════════════════════════════════════
# 总入口
# ═══════════════════════════════════════════════════════════

def run_cross_holding_analysis(existing_conn=None):
    """
    运行完整的交叉持股分析流程：
    1. 构建矩阵
    2. 计算 QoQ 变动
    3. 排名 Buyers/Sellers
    4. 识别 Initiations/Liquidations
    5. 估算 Turnover proxy
    6. 生成报告

    Args:
        existing_conn: 可选，外部传入的 SQLite 连接（复用，不关闭）
    """
    conn = existing_conn if existing_conn else _get_db()

    logger.info("=== Cross-Holding Analysis ===")

    # 1. 矩阵
    matrix = build_cross_holding_matrix(conn)
    if matrix.empty:
        logger.warning("No cross-holding data. Skipping.")
        if not existing_conn:
            conn.close()
        return

    # 2. QoQ 变动（仅在有两期数据时）
    qoq = compute_qoq_changes(conn)

    # 3. Buyers / Sellers
    if not qoq.empty:
        rank_top_buyers_sellers(conn, "buyers")
        rank_top_buyers_sellers(conn, "sellers")
        find_initiations_liquidations(conn)
        compute_turnover_proxy(conn)

    logger.info("Cross-holding analysis complete.")

    if not existing_conn:
        conn.close()


if __name__ == "__main__":
    run_cross_holding_analysis()
