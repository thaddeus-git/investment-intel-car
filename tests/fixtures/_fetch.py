#!/usr/bin/env python3
"""
Helper: fetch 13F infotable.xml from SEC EDGAR and extract a holding.

NOT a deliverable by itself — used interactively to read the true values
that get baked into ground_truth.yaml. Run:

    python3 tests/fixtures/_fetch.py

Throttled to >=0.15s between requests. User-Agent includes email per SEC policy.
"""
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "thaddeus thaddeus@example.com"
LAST_REQUEST = [0.0]

# ticker -> (nameOfIssuer substring, CUSIP) for the competitors we care about.
# CUSIPs verified against SEC infotable XML. ⚠️ 旧 config 的 CUSIP 多处错配
# （审计 memory 标注），所以匹配优先用 name substring，CUSIP 仅作辅助。
# 带 ✓ 的 = 已从 SEC XML 实测确认；其余为待核。
TICKER_MATCH = {
    "CVNA": ("CARVANA",      "146869102"),   # ✓
    "KMX":  ("CARMAX",       "143130102"),
    "AN":   ("AUTONATION",   "05329W102"),   # ✓ (旧 025837109 错)
    "LAD":  ("LITHIA",       "53617J108"),
    "PAG":  ("PENSKE",       "708160103"),
    "GPI":  ("GROUP 1",      "362405108"),
    "SAH":  ("SONIC",        "83545G102"),   # ✓ (旧 811045106 错)
    "ABG":  ("ASBURY",       "043458109"),
    "VRM":  ("VROOM",        "92847H106"),
    "OPLN": ("OPENLANE",     "68298W103"),   # ex-KAR / OPENLANE
}

BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def _throttle():
    elapsed = time.time() - LAST_REQUEST[0]
    if elapsed < 0.3:
        time.sleep(0.3 - elapsed)
    LAST_REQUEST[0] = time.time()


def _get(url, retries=4):
    import gzip, time as _time
    last_err = None
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=BASE_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return data
        except Exception as e:
            last_err = e
            _time.sleep(1.0 * (2 ** attempt))  # backoff 1, 2, 4, 8s
    raise last_err


def recent_13f_filings(cik):
    """Return list of (accession_no_dashes, filing_date, report_date) for 13F-HR, newest first."""
    padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    d = json.loads(_get(url))
    rec = d["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form == "13F-HR":
            acc = rec["accessionNumber"][i]
            out.append({
                "accession": acc,
                "filing_date": rec["filingDate"][i],
                "report_date": rec["reportDate"][i],
                "is_xbrl": rec.get("isXBRL", [0]*len(rec["form"]))[i] if "isXBRL" in rec else None,
            })
    return out, d.get("name", "")


def infotable_url(cik, accession):
    """Find the infotable XML file for an accession via its index.json."""
    numeric = cik.lstrip("0")
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{numeric}/{acc_nodash}/"
    idx = json.loads(_get(base + "index.json"))
    candidates = []
    for item in idx["directory"]["item"]:
        name = item["name"]
        if name.lower().endswith(".xml") and name.lower() != "primary_doc.xml" \
           and "form13f" not in name.lower() and "primary" not in name.lower():
            candidates.append(name)
    # prefer names containing 'infotable' or '13f'
    for n in candidates:
        if "infotable" in n.lower() or "13f" in n.lower():
            return base + n, n
    if candidates:
        return base + candidates[0], candidates[0]
    raise RuntimeError(f"no infotable xml found in {base}; candidates={candidates}")


def parse_holdings(xml_bytes):
    """Parse 13F infotable XML -> list of dicts. Handles both namespaced and bare."""
    # SEC 13F XML is typically not namespaced at infoTable level, but be safe.
    root = ET.fromstring(xml_bytes)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def t(elem, tag):
        el = elem.find(f"{ns}{tag}")
        return el.text.strip() if el is not None and el.text else None

    holdings = []
    for it in root.iter():
        local = it.tag.split("}")[-1]
        if local != "infoTable":
            continue
        name = t(it, "nameOfIssuer")
        cusip = t(it, "cusip")
        value = t(it, "value")
        ssh = it.find(f"{ns}shrsOrPrnAmt")
        sshamt = None
        sshtype = None
        if ssh is not None:
            a = ssh.find(f"{ns}sshPrnamt")
            ty = ssh.find(f"{ns}sshPrnamtType")
            sshamt = a.text.strip() if a is not None and a.text else None
            sshtype = ty.text.strip() if ty is not None and ty.text else None
        put_call = t(it, "putCall")
        holdings.append({
            "nameOfIssuer": name,
            "cusip": cusip,
            "value": int(value) if value else None,
            "shares": int(sshamt) if sshamt else None,
            "sshPrnamtType": sshtype,
            "putCall": put_call,
        })
    return holdings


def find_holding(holdings, ticker=None, cusip=None, name_substr=None):
    """Match by CUSIP (preferred) and/or name substring. Returns all matching rows
    (a single position is often split across multiple sub-advisor <infoTable> rows)."""
    if ticker in TICKER_MATCH:
        default_name, default_cusip = TICKER_MATCH[ticker]
        name_substr = name_substr or default_name
        cusip = cusip or default_cusip
    matches = []
    for h in holdings:
        nm = (h["nameOfIssuer"] or "").upper()
        if cusip and h["cusip"] == cusip:
            matches.append(h)
        elif name_substr and name_substr.upper() in nm:
            matches.append(h)
    # dedupe exact duplicates (same cusip+value+shares)
    seen = set()
    out = []
    for h in matches:
        k = (h["cusip"], h["value"], h["shares"], h["putCall"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def aggregate(rows):
    """Sum value and shares across sub-advisor rows; compute implied price for unit detection."""
    # Only sum SH (share) rows; put/call options are not shares.
    sh_rows = [r for r in rows if (r["sshPrnamtType"] or "SH") == "SH"]
    value_sum = sum(r["value"] for r in sh_rows if r["value"])
    shares_sum = sum(r["shares"] for r in sh_rows if r["shares"])
    implied = (value_sum / shares_sum) if shares_sum else None
    return {
        "n_rows": len(sh_rows),
        "value_sum": value_sum,
        "shares_sum": shares_sum,
        "implied_price": implied,
    }


def fetch_holding(cik, accession, ticker):
    url, fname = infotable_url(cik, accession)
    xml = _get(url)
    holdings = parse_holdings(xml)
    matches = find_holding(holdings, ticker=ticker)
    return {
        "infotable_url": url,
        "infotable_filename": fname,
        "n_holdings": len(holdings),
        "matches": matches,
    }


if __name__ == "__main__":
    # quick smoke: Vanguard CVNA 2025-12-31
    cik = "0000102909"
    filings, name = recent_13f_filings(cik)
    print(f"== {name} ({cik}) ==")
    for f in filings[:6]:
        print(" ", f["accession"], f["filing_date"], "report=", f["report_date"])
    f0 = filings[0]
    res = fetch_holding(cik, f0["accession"], "CVNA")
    print("\ninfotable:", res["infotable_url"])
    print("total holdings in file:", res["n_holdings"])
    for m in res["matches"]:
        implied = (m["value"] / m["shares"]) if m["shares"] else None
        print(f"  CVNA: name={m['nameOfIssuer']!r} value={m['value']} shares={m['shares']} "
              f"type={m['sshPrnamtType']} putCall={m['putCall']} implied_price=${implied:.4f}")
