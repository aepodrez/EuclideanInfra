"""XBRL-to-Compustat field mapper using Bedrock LLM + accounting identity validation.

Entry point: map_filing(cik, accession, form_type, ticker, sic, bedrock_client)
Returns a flat dict of Compustat field values for one filing, ready to write as parquet.

Uses the SEC EDGAR companyfacts REST API (no edgartools dependency) so it is
lightweight enough to run inside a Lambda function.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

EDGAR_IDENTITY = "EuclideanResearch contact@example.com"

# ---------------------------------------------------------------------------
# Compustat field catalogue fed to the LLM
# ---------------------------------------------------------------------------
COMPUSTAT_FIELDS: dict[str, str] = {
    # Balance Sheet — Assets
    "at":        "Total Assets",
    "act":       "Total Current Assets",
    "che":       "Cash and Cash Equivalents (pure cash only, NOT including short-term investments)",
    "rect":      "Accounts Receivable Net (trade receivables; for banks use net loans)",
    "invt":      "Inventory Net",
    "ivst":      "Short-term Investments / Marketable Securities (current)",
    "xpp":       "Prepaid Expenses (current)",
    "aco":       "Other Current Assets",
    "ppent":     "Property Plant & Equipment Net (EXCLUDE operating lease ROU assets)",
    "ppent_rou": "Operating Lease Right-of-Use Assets (ASC 842, post-2019)",
    "intan":     "Intangible Assets Net (EXCLUDING goodwill)",
    "gdwl":      "Goodwill",
    "ivao":      "Long-term Investments / Noncurrent Investments",
    # Balance Sheet — Liabilities
    "lt":            "Total Liabilities",
    "lct":           "Total Current Liabilities",
    "lt_noncurrent": "Total Noncurrent Liabilities",
    "dlc":           "Short-term Debt (current maturities of LT debt + short-term borrowings)",
    "dltt":          "Long-term Debt Noncurrent",
    "ap":            "Accounts Payable (trade)",
    "txp":           "Income Taxes Payable (current)",
    "drc":           "Deferred Revenue (current)",
    "xacc":          "Accrued Liabilities / Accrued Expenses (current)",
    "lco":           "Other Current Liabilities",
    "lo":            "Other Noncurrent Liabilities",
    # Balance Sheet — Equity
    "seq":  "Total Stockholders Equity (including NCI/minority interest) — used in balance sheet identity",
    "ceq":  "Common Equity attributable to parent shareholders",
    "pstk": "Preferred Stock carrying value",
    "re":   "Retained Earnings (Accumulated Deficit)",
    "mib":  "Minority / Noncontrolling Interest (balance sheet carrying value)",
    # Income Statement
    "sale":   "Net Revenue / Net Sales (operating revenue only; for banks use total interest + noninterest income)",
    "cogs":   "Cost of Goods Sold / Cost of Revenue",
    "gp":     "Gross Profit",
    "xsga":   "Selling General & Administrative Expense",
    "xrd":    "Research & Development Expense",
    "xad":    "Advertising Expense",
    "xint":   "Interest Expense",
    "dp":     "Depreciation & Amortization",
    "oiadp":  "Operating Income / Loss (AFTER D&A)",
    "nopi":   "Non-operating Income (Expense) net",
    "pi":     "Pre-tax Income (Income Before Income Taxes)",
    "txt":    "Income Tax Expense (Benefit)",
    "mib_ni": "Net Income attributable to Noncontrolling Interest (NCI income flow)",
    "ib":     "Net Income including NCI / Net Income (Loss) total",
    "ni":     "Net Income attributable to parent / common shareholders",
    # Cash Flow
    "oancf":  "Net Cash from Operating Activities",
    "ivncf":  "Net Cash from Investing Activities",
    "fincf":  "Net Cash from Financing Activities",
    "exre":   "Effect of Exchange Rate Changes on Cash",
    "capx":   "Capital Expenditures (payments to acquire PP&E)",
    "dvc":    "Common Dividends Paid",
    "dvp":    "Preferred Dividends Paid",
    "prstkc": "Common Stock Repurchases",
    "scstkc": "Proceeds from Issuance of Common Stock",
    "dltis":  "Proceeds from Issuance of Long-term Debt",
    "dltr":   "Repayments of Long-term Debt",
}

# Accounting identities shown to the LLM as reasoning constraints
IDENTITY_PROMPT = """
Accounting identities to guide your tag selection (all values in same currency units):
  1. at = lt + seq               (Total Assets = Total Liabilities + Total Equity)
  2. gp = sale - cogs            (Gross Profit = Revenue - Cost of Revenue)
  3. ib ≈ pi - txt - mib_ni     (Net Income ≈ Pre-tax Income - Tax - Minority NCI income)
  4. oancf + ivncf + fincf + exre ≈ change in cash balance (Cash Flow Statement)

Use these as sanity checks: if your chosen tags violate an identity badly, reconsider.
"""

# ---------------------------------------------------------------------------
# Industry classification
# ---------------------------------------------------------------------------
def classify_industry(sic: Optional[str]) -> str:
    if not sic:
        return "GENERAL"
    try:
        s = int(str(sic).strip())
    except (ValueError, TypeError):
        return "GENERAL"
    if 6000 <= s <= 6099:
        return "BANK"
    if 6300 <= s <= 6411:
        return "INSURANCE"
    if s == 6798:
        return "REIT"
    if 6100 <= s <= 6299 or 6700 <= s <= 6726:
        return "FINANCIAL"
    if 4900 <= s <= 4999:
        return "UTILITY"
    return "GENERAL"

# ---------------------------------------------------------------------------
# SEC EDGAR REST API helpers
# ---------------------------------------------------------------------------
def _http_get_json(url: str, retries: int = 3, backoff: float = 2.0) -> dict:
    headers = {
        "User-Agent": EDGAR_IDENTITY,
        "Accept": "application/json",
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
            else:
                raise


def fetch_filing_metadata(cik: str, accession: str) -> dict:
    """Returns {report_date, form_type, filed_date} for a given CIK + accession."""
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    data = _http_get_json(url)

    accession_norm = accession.replace("-", "")
    recent = data.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])

    for i, acc in enumerate(accessions):
        if acc.replace("-", "") == accession_norm:
            return {
                "report_date": recent.get("reportDate", [""])[i],
                "form_type":   recent.get("form", [""])[i],
                "filed_date":  recent.get("filingDate", [""])[i],
            }

    raise ValueError(f"Accession {accession} not found in submissions for CIK {cik}")


def fetch_xbrl_facts(cik: str, accession: str) -> dict[str, float]:
    """Returns {us-gaap concept: value} for all monetary USD facts in this filing."""
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    data = _http_get_json(url)

    accession_norm = accession.replace("-", "")
    facts: dict[str, float] = {}
    us_gaap = data.get("facts", {}).get("us-gaap", {})

    for concept, info in us_gaap.items():
        usd_entries = info.get("units", {}).get("USD", [])
        for entry in usd_entries:
            if entry.get("accn", "").replace("-", "") == accession_norm:
                # Prefer FY/annual facts; for the same concept take the one with
                # the longest period (largest val is a heuristic for annual vs. quarterly)
                val = entry.get("val")
                if val is not None:
                    # Keep the first match; annual filings have fp="FY"
                    fp = entry.get("fp", "")
                    if concept not in facts or fp == "FY":
                        facts[concept] = float(val)

    log.info("Fetched %d XBRL facts for accession %s", len(facts), accession)
    return facts

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def _fmt_value(v: float) -> str:
    """Format a dollar value compactly for the prompt."""
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v >= 1e12:
        return f"{sign}{abs_v/1e12:.2f}T"
    if abs_v >= 1e9:
        return f"{sign}{abs_v/1e9:.2f}B"
    if abs_v >= 1e6:
        return f"{sign}{abs_v/1e6:.2f}M"
    if abs_v >= 1e3:
        return f"{sign}{abs_v/1e3:.2f}K"
    return f"{sign}{abs_v:.0f}"


def build_prompt(tag_values: dict[str, float], form_type: str, industry: str) -> str:
    # Sort tags by absolute value descending so the most material items appear first
    sorted_tags = sorted(tag_values.items(), key=lambda x: abs(x[1]), reverse=True)

    tag_lines = "\n".join(
        f"  {concept}: {_fmt_value(val)}" for concept, val in sorted_tags
    )

    field_lines = "\n".join(
        f"  {field}: {desc}" for field, desc in COMPUSTAT_FIELDS.items()
    )

    industry_note = (
        f"\nNote: This is a {industry} industry company. "
        "Bank revenue (sale) = total interest income + non-interest income. "
        "For banks, rect = net loans receivable. "
        if industry in ("BANK", "FINANCIAL")
        else ""
    )

    return f"""You are mapping XBRL tags from a SEC {form_type} filing to Compustat financial data fields.

XBRL tags present in this filing (tag: USD value):
{tag_lines}

Map each Compustat field below to exactly ONE XBRL tag from the list above.
Return null for a field if no appropriate tag is present.
{industry_note}
{IDENTITY_PROMPT}

Compustat fields to map:
{field_lines}

Rules:
- Choose the tag that BEST semantically matches the field description.
- Prefer specific tags over generic parent tags when both are present.
- Do NOT map the same XBRL tag to more than one field (except seq and ceq may share StockholdersEquity tags if needed).
- For composite tags that bundle multiple fields (e.g. CashCashEquivalentsAndShortTermInvestments), prefer more specific tags.
- Return ONLY a valid JSON object on a single line. No explanation, no markdown, no code block.

Example format:
{{"at": "Assets", "sale": "Revenues", "ib": "NetIncomeLoss", "oancf": null}}

JSON mapping:"""


# ---------------------------------------------------------------------------
# Bedrock invocation
# ---------------------------------------------------------------------------
_BEDROCK_MODEL = "us.deepseek.r1-v1:0"


def invoke_bedrock(prompt: str, bedrock_client) -> dict[str, Optional[str]]:
    """Calls Bedrock and returns the parsed {field: xbrl_tag_or_null} mapping."""
    response = bedrock_client.converse(
        modelId=_BEDROCK_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 2048, "temperature": 0},
    )
    text = ""
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            text = block["text"].strip()
            break
    if not text:
        raise ValueError(f"No text block in Bedrock response: {response['output']['message']['content']}")

    # Extract JSON — the model sometimes wraps in markdown despite instructions
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        raise ValueError(f"No JSON object found in Bedrock response: {text[:200]}")

    raw = json.loads(json_match.group(0))
    # Normalise: keep only known fields, coerce None
    return {
        field: (raw[field] if raw.get(field) else None)
        for field in COMPUSTAT_FIELDS
        if field in raw
    }


# ---------------------------------------------------------------------------
# Value extraction + anchor validation
# ---------------------------------------------------------------------------
def extract_values(facts: dict[str, float], mapping: dict[str, Optional[str]]) -> dict[str, float]:
    """Apply the AI mapping to get numeric values for each Compustat field."""
    values: dict[str, float] = {}
    for field, tag in mapping.items():
        if tag and tag in facts:
            values[field] = facts[tag]
    return values


def _pct_err(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if actual == 0 else float("inf")
    return abs(actual - expected) / abs(expected)


def validate_anchors(values: dict[str, float]) -> dict[str, float]:
    """Returns {anchor_name: relative_residual}. 0 = perfect, >0.05 = suspect."""
    residuals: dict[str, float] = {}

    at  = values.get("at",  0.0)
    lt  = values.get("lt",  0.0)
    seq = values.get("seq", 0.0)
    if at and lt and seq:
        residuals["BalanceSheet"] = _pct_err(at, lt + seq)

    sale = values.get("sale", 0.0)
    cogs = values.get("cogs", 0.0)
    gp   = values.get("gp",   0.0)
    if sale and cogs and gp:
        residuals["GrossProfit"] = _pct_err(gp, sale - cogs)

    pi     = values.get("pi",     0.0)
    txt    = values.get("txt",    0.0)
    mib_ni = values.get("mib_ni", 0.0)
    ib     = values.get("ib",     0.0)
    if pi and ib:
        residuals["NetIncome"] = _pct_err(ib, pi - txt - mib_ni)

    oancf = values.get("oancf", 0.0)
    ivncf = values.get("ivncf", 0.0)
    fincf = values.get("fincf", 0.0)
    if oancf or ivncf or fincf:
        residuals["CashFlow_componentSum"] = oancf + ivncf + fincf

    return residuals


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def map_filing(
    cik: str,
    accession: str,
    form_type: str,
    ticker: str,
    sic: Optional[str] = None,
    bedrock_client=None,
) -> dict:
    """
    Fetch XBRL facts for one filing, ask Bedrock to map tags to Compustat fields,
    validate accounting identities, and return a flat row dict.

    The returned dict can be written directly as a single-row parquet file.
    """
    cik_padded = str(int(cik)).zfill(10)

    log.info("map_filing: cik=%s accession=%s form=%s ticker=%s", cik, accession, form_type, ticker)

    # 1. Filing metadata (report_date)
    metadata = fetch_filing_metadata(cik_padded, accession)
    report_date = metadata.get("report_date", "")
    log.info("report_date=%s", report_date)

    # 2. XBRL facts
    facts = fetch_xbrl_facts(cik_padded, accession)
    if not facts:
        raise RuntimeError(f"No XBRL facts found for {cik} / {accession}")

    # 3. Industry
    industry = classify_industry(sic)

    # 4. AI mapping
    prompt = build_prompt(facts, form_type, industry)
    mapping = invoke_bedrock(prompt, bedrock_client)
    log.info("AI mapping produced %d field assignments", sum(1 for v in mapping.values() if v))

    # 5. Extract numeric values
    values = extract_values(facts, mapping)

    # 6. Validate
    residuals = validate_anchors(values)
    log.info("anchor residuals: %s", {k: f"{v:.3f}" for k, v in residuals.items() if isinstance(v, float)})

    # 7. Build output row
    row: dict = {
        "cik":       cik_padded,
        "ticker":    ticker,
        "datadate":  report_date,
        "form_type": form_type,
        "industry":  industry,
    }
    row.update(values)
    row["_anchor_residuals"] = json.dumps(residuals)
    row["_ai_mapping"]       = json.dumps(mapping)

    return row
