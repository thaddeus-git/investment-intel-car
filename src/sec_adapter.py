"""
SEC Adapter 防腐层（Anti-Corruption Layer）
==========================================

业务代码（institutional_tracker.py / cross_holding.py）与脏 SEC 数据之间的一层防御。

为什么需要这一层 —— 见 memory [[audit-2026-06-19-critical-issues]] / [[sec-adapter-design]]：

| 问题 | 根因 | adapter 做的事 |
|------|------|---------------|
| B1 单位混存 | SEC `<value>` 单位因申报人而异（USD 或 $1000s），edgartools 原样返回 | 出口 `_normalize_value` 用「隐含股价 = value/shares」判断单位并归一化 |
| B5 CIK 错配 | `Company(cik)` 对任何 CIK 都返回某 entity，不验证 | 进口 `verify_institution_cik` 调 submissions JSON 验真，错就 raise |
| B6 7 家零数据 | `ThirteenF()` 抛 `'NoneType' object has no attribute 'find'` 被裸 except 吞 | 逐 filing 包住 AttributeError/TypeError/NoneType，log accession 后 continue |

**职责边界**：adapter 只负责「取 + 验 + 归一化」，不写 DB。写库留给 institutional_tracker（T3）。

后端
----
默认 `backend="direct"`：直接调 SEC submissions JSON + infotable XML（urllib），
这正是 tests/fixtures/_fetch.py 已用 7/7 金标准验证过的稳健路径，绕开 edgartools
的 NoneType bug。

`backend="edgartools"`：走 edgartools `ThirteenF()`，但其异常被本层包住——留给仍想
用 edgartools 的调用方，且用于单元测试「包住 NoneType」契约。
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认 User-Agent（SEC 政策要求含联系方式）。正式交付前换成真实邮箱
# （审计 Minor 项，见 [[audit-2026-06-19-critical-issues]]）。
DEFAULT_USER_AGENT = "CompetitorIntel/1.0 (thaddeus@example.com)"

# 「活跃 13F 申报人」判定窗口。13F 季度申报人正常每 ~3 个月一份；超过此窗口无新 13F
# 即视为已停止申报（如 BlackRock Finance 旧 CIK 0001364742，最后一份 2024-08）。
# 设计 memory 写的是「最近 2 年」，但 2 年(730d) 放过了 0001364742（22 个月前还在报），
# 抓不住 B5；改成 18 个月(548d) 既能抓住停止申报的子公司，又对正常申报人留足余量。
RECENT_13F_WITHIN_DAYS = 548


# ═══════════════════════════════════════════════════════════
# 自定义异常
# ═══════════════════════════════════════════════════════════

class SECAdapterError(Exception):
    """adapter 层所有异常的基类。"""


class InvalidCIKError(SECAdapterError):
    """CIK 不存在、或不是活跃 13F 申报人（B5：子公司/已停报的旧 CIK）。"""


class SuspiciousValueError(SECAdapterError):
    """归一化后隐含股价异常（>$1000），疑似数据错误。"""


class ThirteenFParseError(SECAdapterError):
    """单份 13F 解析失败（含 edgartools NoneType / AttributeError / TypeError）。"""


# ═══════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════

@dataclass
class InstitutionMeta:
    """已验证的机构元数据（verify_institution_cik 的返回）。"""
    cik: str                       # 10 位 padded CIK
    name: str                      # SEC entity 真实名
    has_recent_13f: bool           # 最近窗口内是否有 13F-HR 申报
    last_filing_date: Optional[str]   # 最近一份 13F-HR 的 filing_date (YYYY-MM-DD)
    last_report_date: Optional[str]   # 最近一份 13F-HR 的 reportDate (YYYY-MM-DD)


@dataclass
class Holding:
    """一条已归一化的持仓（机构 × 标的 × 报告期）。"""
    institution_cik: str
    institution_name: str
    ticker: str                    # 竞品 ticker（已匹配）
    issuer_name: str               # SEC infotable 里的 nameOfIssuer
    cusip: str
    value_usd: int                 # 归一化后的美元整数（B1 修复点）
    shares: int                    # SH 子行求和后的股数
    report_period: str             # 13F reportDate (YYYY-MM-DD)
    filing_date: str               # 13F filing_date
    accession_number: str          # 失败可追溯
    unit: str                      # "USD" 或 "1000s"（归一化前的原始单位判定）
    n_sub_rows: int = 1            # 同一标的被拆成几个 sub-advisor 子行求和
    share_type: str = "SH"
    put_call: Optional[str] = None
    sanity_warnings: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 竞品匹配表
# ═══════════════════════════════════════════════════════════
# 13F infotable 只有 nameOfIssuer + CUSIP，没有 ticker。这里维护 ticker → (名称子串, CUSIP)。
# CUSIP 标 ✓ 的已从 SEC XML 实测确认（tests/fixtures/_fetch.py）；其余为待核，匹配优先
# 走 CUSIP，CUSIP 命中不上再退回名称子串。⚠️ 旧 config 的 CUSIP 多处错配（见审计 memory）。
COMPETITOR_MATCH: Dict[str, Tuple[str, Optional[str]]] = {
    "CVNA": ("CARVANA",     "146869102"),   # ✓
    "KMX":  ("CARMAX",      "143130102"),
    "AN":   ("AUTONATION",  "05329W102"),   # ✓
    "LAD":  ("LITHIA",      "53617J108"),
    "PAG":  ("PENSKE",      "708160103"),
    "GPI":  ("GROUP 1",     "362405108"),
    "SAH":  ("SONIC",       "83545G102"),   # ✓
    "ABG":  ("ASBURY",      "043458109"),
    "CARG": ("CARGURUS",    None),
    "CARS": ("CARS.COM",    None),
    "TRUE": ("TRUECAR",     None),
    "KAR":  ("OPENLANE",    "68298W103"),   # ex-KAR，2025-01 更名 OPENLANE
    "ACVA": ("ACV AUCTIONS", None),
    "RUSHA":("RUSH ENTERPRISES", None),
    "UXIN": ("UXIN",        None),
    "ATHM": ("AUTOHOME",    None),
    "VRM":  ("VROOM",       "92847H106"),
}


def _default_competitors() -> List[str]:
    """从 config 读竞品 ticker（config 可能不在 sys.path 时退回内置表）。"""
    try:
        from config import COMPETITORS  # type: ignore
        return [c["ticker"] for c in COMPETITORS]
    except Exception:
        return list(COMPETITOR_MATCH.keys())


# ═══════════════════════════════════════════════════════════
# SECAdapter
# ═══════════════════════════════════════════════════════════

class SECAdapter:
    """SEC 数据防腐层。取 + 验 + 归一化，不写 DB。"""

    def __init__(
        self,
        competitors: Optional[List[str]] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        throttle_seconds: float = 0.3,
        recent_13f_within_days: int = RECENT_13F_WITHIN_DAYS,
        backend: str = "direct",
    ):
        self.competitors = competitors or _default_competitors()
        self.user_agent = user_agent
        self.throttle_seconds = throttle_seconds
        self.recent_13f_within_days = recent_13f_within_days
        self.backend = backend
        # 反查表：cusip -> ticker（仅已配 CUSIP 的竞品）
        self._cusip_to_ticker: Dict[str, str] = {
            cusip: ticker for ticker, (_, cusip) in COMPETITOR_MATCH.items() if cusip
        }
        self._last_request = 0.0

    # ── HTTP 基础 ───────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.throttle_seconds:
            time.sleep(self.throttle_seconds - elapsed)
        self._last_request = time.time()

    def _get(self, url: str, retries: int = 4) -> bytes:
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            self._throttle()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                return data
            except urllib.error.HTTPError as e:
                # 404 等不重试，直接抛给上层判断
                raise
            except Exception as e:  # 网络抖动 / 超时 → 指数退避
                last_err = e
                time.sleep(1.0 * (2 ** attempt))
        raise SECAdapterError(f"GET {url} failed after {retries} retries: {last_err}")

    def _get_json(self, url: str) -> dict:
        return json.loads(self._get(url))

    # ── CIK 工具 ───────────────────────────────────────────

    @staticmethod
    def _pad_cik(cik: str) -> str:
        return cik.lstrip("0").zfill(10)

    # ═══════════════════════════════════════════════════════════
    # 1. verify_institution_cik
    # ═══════════════════════════════════════════════════════════

    def verify_institution_cik(self, cik: str) -> InstitutionMeta:
        """
        进 = 任何字符串，出 = 已验证的机构元数据。
        CIK 不存在 / 不是活跃 13F 申报人 → raise InvalidCIKError。绝不静默返回错误数据。
        """
        padded = self._pad_cik(cik)
        url = f"https://data.sec.gov/submissions/CIK{padded}.json"
        try:
            d = self._get_json(url)
        except urllib.error.HTTPError as e:
            raise InvalidCIKError(
                f"CIK {cik} (padded {padded}): SEC submissions JSON returned HTTP {e.code} — "
                f"CIK 不存在或不可访问"
            ) from e

        name = d.get("name", "") or ""
        filings_13f = self._extract_13f_filings(d)

        if not filings_13f:
            # 合法 entity 但根本不报 13F（如竞品公司 CIK、子公司）→ 对机构用途而言无效
            raise InvalidCIKError(
                f"CIK {cik} ({name!r}): 无任何 13F-HR 申报，不是 13F 机构申报人"
            )

        latest = filings_13f[0]
        has_recent = self._is_recent(latest["filing_date"])

        if not has_recent:
            raise InvalidCIKError(
                f"CIK {cik} ({name!r}): 最近一份 13F-HR 申报 {latest['filing_date']} "
                f"超出 {self.recent_13f_within_days}d 活跃窗口——疑似已停止申报的旧 CIK/"
                f"子公司（对照 B5 BlackRock Finance 0001364742）"
            )

        return InstitutionMeta(
            cik=padded,
            name=name,
            has_recent_13f=True,
            last_filing_date=latest["filing_date"],
            last_report_date=latest["report_date"],
        )

    def _is_recent(self, filing_date: Optional[str]) -> bool:
        if not filing_date:
            return False
        try:
            fd = datetime.strptime(filing_date, "%Y-%m-%d")
        except ValueError:
            return False
        return (datetime.now() - fd).days <= self.recent_13f_within_days

    @staticmethod
    def _extract_13f_filings(submissions: dict) -> List[dict]:
        """从 submissions JSON 的 recent 区取 13F-HR，按时间倒序返回。"""
        rec = submissions.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        out = []
        for i, form in enumerate(forms):
            if form == "13F-HR":
                out.append({
                    "accession": rec["accessionNumber"][i],
                    "filing_date": rec["filingDate"][i],
                    "report_date": rec["reportDate"][i],
                })
        return out

    # ═══════════════════════════════════════════════════════════
    # 2. fetch_13f_holdings
    # ═══════════════════════════════════════════════════════════

    def fetch_13f_holdings(self, cik: str, periods: int = 4) -> List[Holding]:
        """
        拉取该机构最近 `periods` 份 13F-HR，返回匹配到竞品的、已归一化的 Holding 列表。
        单份 filing 解析失败（含 edgartools NoneType/AttributeError/TypeError）→ log warning
        + 记录 accession 后 continue，不影响其它 filing。
        """
        padded = self._pad_cik(cik)
        filings, name = self._recent_13f_filings(padded)
        filings = filings[:periods]
        if not filings:
            logger.warning("CIK %s (%s): 无 13F-HR 申报可取", padded, name)
            return []

        holdings: List[Holding] = []
        for fl in filings:
            try:
                rows = self._parse_filing_into_rows(padded, fl)
            except ThirteenFParseError as e:
                # B6 核心：包住 edgartools NoneType 等，记 accession，继续
                logger.warning(
                    "CIK %s accession %s (report %s) 解析失败，已跳过: %s",
                    padded, fl["accession"], fl.get("report_date"), e,
                )
                continue
            except (AttributeError, TypeError) as e:
                # 兜底：任何 NoneType / 属性缺失都归到这里，绝不抛穿
                logger.warning(
                    "CIK %s accession %s 抛出 %s（已包住跳过）: %s",
                    padded, fl["accession"], type(e).__name__, e,
                )
                continue

            for h in self._rows_to_holdings(rows, padded, name, fl):
                h.sanity_warnings = self.sanity_check_holding(h)
                holdings.append(h)

        logger.info("CIK %s (%s): %d 份 filing → %d 条竞品持仓", padded, name, len(filings), len(holdings))
        return holdings

    def _recent_13f_filings(self, padded_cik: str) -> Tuple[List[dict], str]:
        url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
        d = self._get_json(url)
        return self._extract_13f_filings(d), d.get("name", "")

    # ── 单份 filing → 原始行 ───────────────────────────────

    def _parse_filing_into_rows(self, cik: str, filing: dict) -> List[dict]:
        """根据 backend 选择解析路径。返回 raw 行 dict 列表。"""
        if self.backend == "edgartools":
            return self._parse_filing_edgartools(cik, filing)
        return self._parse_filing_direct(cik, filing)

    def _parse_filing_direct(self, cik: str, filing: dict) -> List[dict]:
        """直接抓 infotable XML 解析（稳健，绕开 edgartools NoneType bug）。"""
        accession = filing["accession"]
        url, _fname = self._infotable_url(cik, accession)
        xml = self._get(url)
        try:
            return self._parse_infotable_xml(xml)
        except ET.ParseError as e:
            raise ThirteenFParseError(f"infotable XML 解析失败 {url}: {e}") from e

    def _parse_filing_edgartools(self, cik: str, filing: dict) -> List[dict]:
        """走 edgartools ThirteenF()。其 NoneType/AttributeError/TypeError 由上层包住。"""
        try:
            from edgar import Filing, ThirteenF  # type: ignore
        except ImportError as e:
            raise ThirteenFParseError(f"edgartools 未安装: {e}") from e

        accession = filing["accession"]
        acc_nodash = accession.replace("-", "")
        try:
            f = Filing(form="13F-HR", company=f"CIK {cik}", cik=cik,
                       accession_no=accession, date=filing["filing_date"])
            tf = ThirteenF(f)
        except (AttributeError, TypeError) as e:
            # 典型：'NoneType' object has no attribute 'find'
            raise ThirteenFParseError(
                f"edgartools ThirteenF() 构造失败 accession={accession}: {type(e).__name__}: {e}"
            ) from e

        infotable = getattr(tf, "infotable", None)
        if infotable is None or (hasattr(infotable, "empty") and infotable.empty):
            return []

        rows: List[dict] = []
        for _, row in infotable.iterrows():
            rows.append({
                "nameOfIssuer": str(row.get("Issuer", "") or ""),
                "cusip": str(row.get("Cusip", "") or ""),
                "value": _to_int(row.get("Value")),
                "shares": _to_int(row.get("SharesPrnAmount")),
                "sshPrnamtType": str(row.get("Type", "Shares") or "Shares"),
                "putCall": str(row.get("putCall", "") or "") or None,
            })
        return rows

    # ── infotable XML 解析（与 tests/fixtures/_fetch.py 同源） ──

    def _infotable_url(self, cik: str, accession: str) -> Tuple[str, str]:
        numeric = cik.lstrip("0")
        acc_nodash = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{numeric}/{acc_nodash}/"
        idx = self._get_json(base + "index.json")
        candidates = []
        for item in idx["directory"]["item"]:
            n = item["name"].lower()
            if n.endswith(".xml") and n != "primary_doc.xml" \
               and "form13f" not in n and "primary" not in n:
                candidates.append(item["name"])
        for name in candidates:
            if "infotable" in name.lower() or "13f" in name.lower():
                return base + name, name
        if candidates:
            return base + candidates[0], candidates[0]
        raise ThirteenFParseError(f"accession {accession} 下未找到 infotable XML: {base}")

    @staticmethod
    def _parse_infotable_xml(xml_bytes: bytes) -> List[dict]:
        root = ET.fromstring(xml_bytes)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        def text(elem, tag):
            el = elem.find(f"{ns}{tag}")
            return el.text.strip() if el is not None and el.text else None

        rows: List[dict] = []
        for it in root.iter():
            if it.tag.split("}")[-1] != "infoTable":
                continue
            ssh = it.find(f"{ns}shrsOrPrnAmt")
            sshamt = sshtype = None
            if ssh is not None:
                a = ssh.find(f"{ns}sshPrnamt")
                ty = ssh.find(f"{ns}sshPrnamtType")
                sshamt = a.text.strip() if a is not None and a.text else None
                sshtype = ty.text.strip() if ty is not None and ty.text else None
            rows.append({
                "nameOfIssuer": text(it, "nameOfIssuer") or "",
                "cusip": text(it, "cusip") or "",
                "value": _to_int(text(it, "value")),
                "shares": _to_int(sshamt),
                "sshPrnamtType": sshtype or "SH",
                "putCall": text(it, "putCall"),
            })
        return rows

    # ── 原始行 → Holding（匹配 + 聚合 + 归一化） ───────────

    def _match_ticker(self, name: str, cusip: str) -> Optional[str]:
        """nameOfIssuer + cusip → 竞品 ticker。优先 CUSIP，其次名称子串。"""
        if cusip and cusip in self._cusip_to_ticker:
            return self._cusip_to_ticker[cusip]
        up = (name or "").upper()
        for ticker in self.competitors:
            sub, _ = COMPETITOR_MATCH.get(ticker, ("", None))
            if sub and sub.upper() in up:
                return ticker
        return None

    def _rows_to_holdings(
        self, rows: List[dict], cik: str, inst_name: str, filing: dict
    ) -> List[Holding]:
        """按 ticker 聚合 SH 子行，归一化后产出 Holding。"""
        # 按 ticker 分组（只取 SH 行做求和；put/call 不算持股）
        grouped: Dict[str, List[dict]] = {}
        issuer_name: Dict[str, str] = {}
        cusip_seen: Dict[str, str] = {}
        put_call_seen: Dict[str, Optional[str]] = {}
        for r in rows:
            ticker = self._match_ticker(r.get("nameOfIssuer", ""), r.get("cusip", ""))
            if not ticker:
                continue
            if (r.get("sshPrnamtType") or "SH") != "SH":
                continue
            grouped.setdefault(ticker, []).append(r)
            issuer_name.setdefault(ticker, r.get("nameOfIssuer", ""))
            cusip_seen.setdefault(ticker, r.get("cusip", ""))
            put_call_seen.setdefault(ticker, r.get("putCall"))

        holdings: List[Holding] = []
        for ticker, grp in grouped.items():
            value_sum = sum(r["value"] for r in grp if r.get("value"))
            shares_sum = sum(r["shares"] for r in grp if r.get("shares"))
            if value_sum <= 0 and shares_sum <= 0:
                continue
            value_usd = self._normalize_value(value_sum, shares_sum, ticker)
            unit = "1000s" if (shares_sum and value_sum / shares_sum < 1.0) else "USD"
            holdings.append(Holding(
                institution_cik=cik,
                institution_name=inst_name,
                ticker=ticker,
                issuer_name=issuer_name[ticker],
                cusip=cusip_seen[ticker],
                value_usd=value_usd,
                shares=int(shares_sum),
                report_period=filing.get("report_date", ""),
                filing_date=filing.get("filing_date", ""),
                accession_number=filing.get("accession", ""),
                unit=unit,
                n_sub_rows=len(grp),
                share_type="SH",
                put_call=put_call_seen[ticker],
            ))
        return holdings

    # ═══════════════════════════════════════════════════════════
    # 3. _normalize_value（核心）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_value(value: float, shares: float, ticker: str) -> int:
        """
        归一化为美元整数。原理：implied_price = value / shares。

        - implied < $1.0   → <value> 单位是 $1000s，×1000
        - implied $1–$1000 → <value> 已是美元，不变
        - implied > $1000  → 价格异常，raise SuspiciousValueError
        - shares <= 0      → 无法判定单位，按原值返回
        """
        if shares <= 0:
            return int(value)
        implied_price = value / shares
        if implied_price < 1.0:
            return int(value * 1000)
        elif implied_price <= 1000:
            return int(value)
        else:
            raise SuspiciousValueError(
                f"{ticker}: implied price ${implied_price:.2f} out of bounds "
                f"[shares={shares}, value={value}]"
            )

    # ═══════════════════════════════════════════════════════════
    # 4. sanity_check_holding
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def sanity_check_holding(h: Holding) -> List[str]:
        """返回告警列表（空 = 通过）。护栏见 tests/fixtures/ground_truth.yaml sanity_bounds。"""
        warnings: List[str] = []
        if h.value_usd > 20_000_000_000:
            warnings.append(f"value_usd ${h.value_usd:,} 超过单机构单股 $20B 上限")
        if h.value_usd < 1:
            warnings.append(f"value_usd ${h.value_usd} < $1 下限")
        if h.shares <= 0:
            warnings.append(f"shares {h.shares} <= 0")
        return warnings


def _to_int(v) -> Optional[int]:
    """宽松转 int（容忍 None / 空串 / 浮点）。"""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
