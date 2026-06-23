"""
SEC Adapter 单元测试。

分两类：
- 纯逻辑测试（不打网）：_normalize_value 的三种分支、sanity_check、ticker 匹配、
  异常包住逻辑（monkeypatch 模拟 edgartools NoneType）。
- 联网金标准测试：对 tests/fixtures/ground_truth.yaml 的 7 条 fixture，adapter 实拉
  SEC 数据后归一化的 value_usd / shares 应在 ±tolerance 内匹配 expected。

联网测试默认运行（fixture build 时已验过 7/7 VALID，SEC 历史归档不修订）。
离线环境可 `SKIP_NETWORK=1 pytest`。
"""
import os
import sys
from pathlib import Path

import pytest

from sec_adapter import (
    Holding,
    InvalidCIKError,
    SECAdapter,
    SuspiciousValueError,
)

HERE = Path(__file__).resolve().parent
SKIP_NETWORK = os.environ.get("SKIP_NETWORK") == "1"
needs_network = pytest.mark.skipif(SKIP_NETWORK, reason="SKIP_NETWORK=1")


# ─── 加载金标准 fixture ───────────────────────────────────────
def _load_goldens():
    import yaml
    with open(HERE / "fixtures" / "ground_truth.yaml") as fh:
        return yaml.safe_load(fh)["golden_holdings"]


GOLDENS = _load_goldens()
GOLDEN_BY_ID = {g["id"]: g for g in GOLDENS}


# ═══════════════════════════════════════════════════════════
# _normalize_value 三分支
# ═══════════════════════════════════════════════════════════

class TestNormalizeValue:
    def test_normalize_trowe_style(self):
        # T. Rowe Price CVNA 2026Q1: raw 5,590,214 / 17,781,707 → implied $0.31 < $1 → ×1000
        # → 5,590,214,000（见 fixture troweprice-cvna-2026q1）
        out = SECAdapter._normalize_value(5_590_214, 17_781_707, "CVNA")
        assert out == 5_590_214_000

    def test_normalize_lsv_style(self):
        # LSV AN 2026Q1: raw 97,193 / 497,760 → implied $0.195 < $1 → ×1000
        out = SECAdapter._normalize_value(97_193, 497_760, "AN")
        assert out == 97_193_000

    def test_normalize_vanguard_style(self):
        # Vanguard CVNA 2025Q4: 7,082,804,283 / 16,783,101 → implied $422 → 不变
        out = SECAdapter._normalize_value(7_082_804_283, 16_783_101, "CVNA")
        assert out == 7_082_804_283

    def test_normalize_state_street_style(self):
        # State Street CVNA 2025Q4: implied $422 → 不变
        out = SECAdapter._normalize_value(2_411_751_034, 5_714_779, "CVNA")
        assert out == 2_411_751_034

    def test_normalize_citadel_small_holding(self):
        # Citadel SAH 2026Q1: 465,247 / 6,785 → implied $68.57 → 不变（过度归一化守卫）
        out = SECAdapter._normalize_value(465_247, 6_785, "SAH")
        assert out == 465_247

    def test_normalize_suspicious_raises(self):
        # 隐含价 > $1000 → raise
        with pytest.raises(SuspiciousValueError):
            SECAdapter._normalize_value(50_000_000, 100, "CVNA")  # implied $500k

    def test_normalize_zero_shares_returns_int(self):
        # shares <= 0 → 无法判定单位，按原值返回
        assert SECAdapter._normalize_value(123_456, 0, "CVNA") == 123_456

    def test_normalize_boundary_exactly_one_dollar(self):
        # implied 恰好 $1.0 → 落在「不变」分支（< 1.0 才 ×1000）
        assert SECAdapter._normalize_value(1_000_000, 1_000_000, "X") == 1_000_000

    def test_normalize_boundary_exactly_thousand(self):
        # implied 恰好 $1000 → 不变（> 1000 才 raise）
        assert SECAdapter._normalize_value(1_000_000, 1_000, "X") == 1_000_000


# ═══════════════════════════════════════════════════════════
# sanity_check_holding
# ═══════════════════════════════════════════════════════════

class TestSanityCheck:
    def _make(self, **kw):
        base = dict(
            institution_cik="0000102909", institution_name="Vanguard", ticker="CVNA",
            issuer_name="CARVANA", cusip="146869102", value_usd=7_082_804_283,
            shares=16_783_101, report_period="2025-12-31", filing_date="2026-01-29",
            accession_number="0000102909-26-000031", unit="USD",
        )
        base.update(kw)
        return Holding(**base)

    def test_normal_holding_passes(self):
        assert SECAdapter.sanity_check_holding(self._make()) == []

    def test_too_large_value_warns(self):
        h = self._make(value_usd=25_000_000_000)
        w = SECAdapter.sanity_check_holding(h)
        assert any("$20B" in x for x in w)

    def test_tiny_value_warns(self):
        h = self._make(value_usd=0)
        w = SECAdapter.sanity_check_holding(h)
        assert any("< $1" in x for x in w)

    def test_zero_shares_warns(self):
        h = self._make(shares=0)
        w = SECAdapter.sanity_check_holding(h)
        assert any("shares" in x for x in w)


# ═══════════════════════════════════════════════════════════
# ticker 匹配
# ═══════════════════════════════════════════════════════════

class TestTickerMatch:
    def test_match_by_cusip(self):
        a = SECAdapter()
        assert a._match_ticker("ANYTHING", "146869102") == "CVNA"
        assert a._match_ticker("WHATEVER", "05329W102") == "AN"

    def test_match_by_name_substring(self):
        a = SECAdapter()
        assert a._match_ticker("CARVANA CO", "") == "CVNA"
        assert a._match_ticker("AUTONATION INC", "") == "AN"

    def test_no_match_returns_none(self):
        a = SECAdapter()
        assert a._match_ticker("APPLE INC", "037833100") is None


# ═══════════════════════════════════════════════════════════
# verify_institution_cik — 联网（错 CIK 抛 / 正确 CIK 返回）
# ═══════════════════════════════════════════════════════════

@needs_network
class TestVerifyCIK:
    def test_verify_invalid_cik_raises(self):
        # B5: 旧 BlackRock CIK 0001364742 = BlackRock Finance, Inc. 子公司，
        # 最后一份 13F-HR = 2024-08-13，超出 548d 活跃窗口 → 应 raise InvalidCIKError
        a = SECAdapter()
        with pytest.raises(InvalidCIKError):
            a.verify_institution_cik("0001364742")

    def test_verify_correct_cik_returns_meta(self):
        # Vanguard 0000102909 = 正确的活跃 13F 申报人
        a = SECAdapter()
        meta = a.verify_institution_cik("0000102909")
        assert meta.cik == "0000102909"
        assert "VANGUARD" in meta.name.upper()
        assert meta.has_recent_13f is True
        assert meta.last_filing_date is not None

    def test_verify_nonexistent_cik_raises(self):
        # 纯不存在的 CIK → HTTP 404 → InvalidCIKError
        a = SECAdapter()
        with pytest.raises(InvalidCIKError):
            a.verify_institution_cik("0009999999")

    def test_verify_correct_blackrock_cik(self):
        # B5 修复后正确 CIK = 0002012383 BlackRock, Inc.
        a = SECAdapter()
        meta = a.verify_institution_cik("0002012383")
        assert "BLACKROCK" in meta.name.upper()
        assert meta.has_recent_13f is True


# ═══════════════════════════════════════════════════════════
# 异常包住（不打网 — monkeypatch）
# ═══════════════════════════════════════════════════════════

class TestExceptionContainment:
    def test_none_type_filing_is_logged_and_skipped(self, caplog, monkeypatch):
        """B6 核心：edgartools 抛 'NoneType' object has no attribute 'find' 时，
        该 filing 被跳过并 log warning，不让整批失败。"""
        import logging
        a = SECAdapter(backend="edgartools")

        # 两份 filing：第一份模拟 NoneType 异常，第二份正常
        filings = [
            {"accession": "BAD-1", "filing_date": "2026-01-01", "report_date": "2025-12-31"},
            {"accession": "OK-1", "filing_date": "2026-01-02", "report_date": "2025-12-31"},
        ]
        monkeypatch.setattr(a, "_recent_13f_filings", lambda cik: (filings, "Test"))

        call_count = {"n": 0}

        def fake_parse(cik, filing):
            call_count["n"] += 1
            if filing["accession"] == "BAD-1":
                # 模拟 edgartools 的 NoneType 异常
                raise AttributeError("'NoneType' object has no attribute 'find'")
            # 第二份：返回一条 CVNA 持仓
            return [{
                "nameOfIssuer": "CARVANA CO", "cusip": "146869102",
                "value": 7_082_804_283, "shares": 16_783_101,
                "sshPrnamtType": "SH", "putCall": None,
            }]

        monkeypatch.setattr(a, "_parse_filing_into_rows", fake_parse)

        with caplog.at_level(logging.WARNING):
            holdings = a.fetch_13f_holdings("0000102909", periods=4)

        # 两份都被尝试
        assert call_count["n"] == 2
        # 坏的跳过，好的保留
        assert len(holdings) == 1
        assert holdings[0].ticker == "CVNA"
        assert holdings[0].value_usd == 7_082_804_283
        # 失败的 accession 进了 warning 日志
        assert any("BAD-1" in r.message for r in caplog.records)

    def test_all_filings_fail_returns_empty(self, caplog, monkeypatch):
        a = SECAdapter(backend="edgartools")
        filings = [{"accession": "BAD-1", "filing_date": "x", "report_date": "x"}]
        monkeypatch.setattr(a, "_recent_13f_filings", lambda cik: (filings, "Test"))

        def fake_parse(cik, filing):
            raise TypeError("some NoneType issue")

        monkeypatch.setattr(a, "_parse_filing_into_rows", fake_parse)
        holdings = a.fetch_13f_holdings("0000102909", periods=4)
        assert holdings == []

    def test_thirteenf_parse_error_is_caught(self, caplog, monkeypatch):
        """ThirteenFParseError（XML 解析失败）也走 warning 路径。"""
        import logging
        from sec_adapter import ThirteenFParseError

        a = SECAdapter()
        filings = [{"accession": "XML-BAD", "filing_date": "x", "report_date": "x"}]
        monkeypatch.setattr(a, "_recent_13f_filings", lambda cik: (filings, "Test"))
        monkeypatch.setattr(
            a, "_parse_filing_into_rows",
            lambda cik, fl: (_ for _ in ()).throw(ThirteenFParseError("xml boom"))
        )
        with caplog.at_level(logging.WARNING):
            assert a.fetch_13f_holdings("0000102909", periods=4) == []
        assert any("XML-BAD" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# 金标准 fixture 端到端（联网）
# ═══════════════════════════════════════════════════════════

@needs_network
class TestGoldenFixtures:
    """对每条 fixture，adapter 拉该机构该期 13F → 找到该 ticker 的 Holding，
    归一化后的 value_usd / shares 应在 ±tolerance_pct 内匹配 expected。"""

    @pytest.fixture(scope="class")
    def adapter(self):
        return SECAdapter()

    @pytest.mark.parametrize("g", GOLDENS, ids=[g["id"] for g in GOLDENS])
    def test_against_golden_fixtures(self, adapter, g):
        holdings = adapter.fetch_13f_holdings(g["institution_cik"], periods=4)
        match = [h for h in holdings
                 if h.ticker == g["ticker"] and h.report_period == g["report_period"]]
        assert match, (
            f"{g['id']}: adapter 未拉到 {g['institution_cik']} 在 {g['report_period']} "
            f"对 {g['ticker']} 的持仓（共拿到 {len(holdings)} 条持仓）"
        )
        h = match[0]

        exp_val = g["expected_value_usd"]
        exp_sh = g["expected_shares"]
        tol = g.get("tolerance_pct", 0.01)

        val_diff = abs(h.value_usd - exp_val) / exp_val if exp_val else 1
        sh_diff = abs(h.shares - exp_sh) / exp_sh if exp_sh else 1
        assert val_diff <= tol, (
            f"{g['id']}: value_usd {h.value_usd:,} vs expected {exp_val:,} "
            f"(Δ{val_diff*100:.3f}% > {tol*100}%) unit={h.unit}"
        )
        assert sh_diff <= tol, (
            f"{g['id']}: shares {h.shares:,} vs expected {exp_sh:,} "
            f"(Δ{sh_diff*100:.3f}% > {tol*100}%)"
        )

    def test_troweprice_unit_detected_as_1000s(self, adapter):
        """B1 关键回归：T. Rowe Price 的 fixture 应被判定为 1000s 单位并 ×1000。"""
        g = GOLDEN_BY_ID["troweprice-cvna-2026q1"]
        holdings = adapter.fetch_13f_holdings(g["institution_cik"], periods=4)
        h = next(x for x in holdings
                 if x.ticker == g["ticker"] and x.report_period == g["report_period"])
        assert h.unit == "1000s"
        assert h.value_usd == g["expected_value_usd"]

    def test_citadel_not_over_normalized(self, adapter):
        """过度归一化守卫：Citadel SAH 小持仓（implied $68）必须不被 ×1000。"""
        g = GOLDEN_BY_ID["citadel-sah-2026q1"]
        holdings = adapter.fetch_13f_holdings(g["institution_cik"], periods=4)
        h = next(x for x in holdings
                 if x.ticker == g["ticker"] and x.report_period == g["report_period"])
        assert h.unit == "USD"
        assert h.value_usd == g["expected_value_usd"]  # 465,247 不是 465,247,000
