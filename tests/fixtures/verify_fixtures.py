#!/usr/bin/env python3
"""
verify_fixtures.py — 从 SEC EDGAR 实时重新抓取，对比 ground_truth.yaml 是否仍然成立。

用途：fixture 会过期（机构提交新 13F、或修订旧 filing）。定期跑这个脚本，
确认每条 fixture 的 (accession, value, shares) 还和 SEC 当前公开的数据一致。

退出码：
  0  全部 fixture 仍有效
  1  至少一条 fixture 失效（值变了 / 期变了 / 抓不到）

运行：
  python3 tests/fixtures/verify_fixtures.py
  python3 tests/fixtures/verify_fixtures.py --verbose
"""
import argparse
import os
import sys
from pathlib import Path

# 让脚本既能被 `python3 tests/fixtures/verify_fixtures.py` 直接跑，也能作为模块跑
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _fetch  # noqa: E402

# PyYAML 是 requirements.txt 里没有的——尽量用，没有就 fallback 到最小解析
try:
    import yaml
    def load_yaml(path):
        with open(path) as fh:
            return yaml.safe_load(fh)
except ImportError:  # pragma: no cover
    yaml = None
    load_yaml = None


def load_fixtures(path):
    if load_yaml is not None:
        return load_yaml(path)
    raise SystemExit(
        "PyYAML 未安装。请 `pip install pyyaml` 后重试，"
        "或用 `python3 -c` 调 _fetch 直接核对单条。"
    )


def fmt_usd(v):
    return f"${v:,.0f}"


def verify_one(g, verbose=False):
    """返回 (status, detail_dict)。
    status ∈ {VALID, VALUE_CHANGED, PERIOD_CHANGED, FETCH_ERROR}"""
    cik = g["institution_cik"]
    ticker = g["ticker"]
    accession = g["source_accession"]
    out = {"id": g["id"], "accession": accession, "ticker": ticker,
           "period_expected": g["report_period"]}

    try:
        # 1. 取该机构最新 13F-HR 列表，确认 fixture 用的 accession 还在
        filings, name = _fetch.recent_13f_filings(cik)
        acc_dates = {f["accession"]: f["report_date"] for f in filings}
        if accession not in acc_dates:
            out["status"] = "PERIOD_CHANGED"
            out["detail"] = (f"accession {accession} 已不在最近 13F-HR 列表 "
                             f"({len(filings)} 期内)。可能被修订或滚出窗口。最新期: "
                             f"{filings[0]['accession']} report={filings[0]['report_date']}")
            return out
        out["period_actual"] = acc_dates[accession]
        if out["period_actual"] != g["report_period"]:
            out["status"] = "PERIOD_CHANGED"
            out["detail"] = (f"report_date 变了: expected {g['report_period']} "
                             f"got {out['period_actual']}")
            return out

        # 2. 重新抓 infotable，按 ticker 聚合所有 SH 子行
        res = _fetch.fetch_holding(cik, accession, ticker)
        rows = res["matches"]
        if not rows:
            out["status"] = "VALUE_CHANGED"
            out["detail"] = (f"infotable 中已无 {ticker} 持仓行（可能修订后剔除）。"
                             f"url={res['infotable_url']}")
            return out
        agg = _fetch.aggregate(rows)
        implied = agg["implied_price"]
        unit = "USD" if (implied and implied >= 1.0) else "1000s"
        value_usd = agg["value_sum"] if unit == "USD" else agg["value_sum"] * 1000

        out["value_usd_actual"] = int(value_usd)
        out["shares_actual"] = agg["shares_sum"]
        out["implied_actual"] = implied
        out["unit_actual"] = unit
        out["n_rows_actual"] = agg["n_rows"]

        # 3. 对比 expected（容忍 tolerance_pct）
        exp_val = g["expected_value_usd"]
        exp_sh = g["expected_shares"]
        tol = g.get("tolerance_pct", 0.01)
        val_diff = abs(int(value_usd) - exp_val) / exp_val if exp_val else 1
        sh_diff = abs(agg["shares_sum"] - exp_sh) / exp_sh if exp_sh else 1

        if val_diff <= tol and sh_diff <= tol:
            out["status"] = "VALID"
            out["detail"] = (f"OK value={fmt_usd(value_usd)} (Δ{val_diff*100:.3f}%) "
                             f"shares={agg['shares_sum']:,} (Δ{sh_diff*100:.3f}%) "
                             f"unit={unit} implied=${implied:.4f}")
        else:
            out["status"] = "VALUE_CHANGED"
            out["detail"] = (f"value expected {fmt_usd(exp_val)} got {fmt_usd(int(value_usd))} "
                             f"(Δ{val_diff*100:.3f}%); shares expected {exp_sh:,} got "
                             f"{agg['shares_sum']:,} (Δ{sh_diff*100:.3f}%); unit={unit}")
        if verbose:
            out["rows"] = rows
        return out

    except Exception as e:
        out["status"] = "FETCH_ERROR"
        out["detail"] = f"{type(e).__name__}: {e}"
        return out


def main():
    ap = argparse.ArgumentParser(description="Verify ground-truth fixtures against live SEC EDGAR")
    ap.add_argument("--fixtures", default=str(HERE / "ground_truth.yaml"))
    ap.add_argument("--verbose", action="store_true", help="打印每条 fixture 的子行明细")
    ap.add_argument("--only", help="只验证指定 id（逗号分隔）")
    args = ap.parse_args()

    data = load_fixtures(args.fixtures)
    goldens = data["golden_holdings"]
    if args.only:
        want = set(s.strip() for s in args.only.split(","))
        goldens = [g for g in goldens if g["id"] in want]

    print(f"验证 {len(goldens)} 条 fixture against SEC EDGAR (User-Agent含邮箱, 限流0.3s)\n")
    counts = {"VALID": 0, "VALUE_CHANGED": 0, "PERIOD_CHANGED": 0, "FETCH_ERROR": 0}
    for g in goldens:
        r = verify_one(g, verbose=args.verbose)
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        marker = "✅" if r["status"] == "VALID" else "❌"
        print(f"{marker} [{r['status']:14s}] {r['id']:28s} {r['detail']}")
        if args.verbose and "rows" in r:
            for row in r["rows"]:
                imp = (row['value']/row['shares']) if row['shares'] else None
                print(f"        row: cusip={row['cusip']} value={row['value']} "
                      f"shares={row['shares']} type={row['sshPrnamtType']} "
                      f"putCall={row['putCall']} implied=${imp}")

    print(f"\n汇总: VALID={counts['VALID']}  VALUE_CHANGED={counts['VALUE_CHANGED']}  "
          f"PERIOD_CHANGED={counts['PERIOD_CHANGED']}  FETCH_ERROR={counts['FETCH_ERROR']}")

    # 单独提示「期变了」——fixture 应被替换为新一期，而不是删掉
    if counts["PERIOD_CHANGED"]:
        print("\n⚠️  有 fixture 的报告期/accession 已变。这通常意味着机构提交了更新的 13F——")
        print("   fixture 本身仍代表「该期历史真相」，可保留用于历史回归；如要追踪最新期，请新增 fixture。")

    sys.exit(0 if (counts["VALUE_CHANGED"] + counts["FETCH_ERROR"]) == 0 else 1)


if __name__ == "__main__":
    main()
