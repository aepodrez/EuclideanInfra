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
    "che":       "Cash and Cash Equivalents — use CashAndCashEquivalentsAtCarryingValue OR CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents (the post-2018 standard cash flow tag); assign this even if the tag name mentions RestrictedCash",
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
    "lt":            "Total Liabilities — use the Liabilities concept. Do NOT use LiabilitiesAndStockholdersEquity (that is total assets, not liabilities alone). Leave null if no standalone Liabilities tag is present",
    "lct":           "Total Current Liabilities",
    "lt_noncurrent": "Total Noncurrent Liabilities",
    "dlc":           "Short-term Debt (current maturities of LT debt + short-term borrowings)",
    "dltt":          "Long-term Debt Noncurrent",
    "ap":            "Accounts Payable (trade)",
    "txp":           "Income Taxes Payable (current)",
    "drc":           "Deferred Revenue (current)",
    "xacc":          "Accrued Liabilities / Accrued Expenses (current) — use AccruedLiabilitiesCurrent, EmployeeRelatedLiabilitiesCurrent, or composite tags like AccountsPayableAndOtherAccruedLiabilitiesCurrent when no standalone accrued tag exists",
    "lco":           "Other Current Liabilities — use OtherLiabilitiesCurrent, OperatingLeaseLiabilityCurrent, or similar current-liability tags not captured by dlc, ap, txp, drc, or xacc",
    "lo":            "Other Noncurrent Liabilities",
    # Balance Sheet — Equity
    "seq":  "Total Stockholders Equity (including NCI/minority interest) — used in balance sheet identity",
    "ceq":  "Common Equity attributable to parent shareholders",
    "pstk": "Preferred Stock carrying value",
    "re":   "Retained Earnings (Accumulated Deficit)",
    "mib":  "Minority / Noncontrolling Interest (balance sheet carrying value) — use NoncontrollingInterestMember or MinorityInterest tags. For limited partnerships using PartnersCapital/LimitedPartnersCapitalAccount, mib should be null (LP structures have no minority interest)",
    # Income Statement
    "sale":          "Net Revenue / Net Sales (operating revenue only; for banks use total interest + noninterest income)",
    "revt_interest": "Interest Income — bank total interest and fee income on loans/securities (banks only)",
    "revt_noninterest": "Non-Interest Income — bank fees, trading gains, service charges (banks only)",
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
    "exre":   "Effect of Exchange Rate Changes on Cash — ONLY use EffectOfExchangeRateOnCash* tags. NEVER use CashCashEquivalentsRestrictedCash*PeriodIncreaseDecreaseIncludingExchangeRateEffect (that is the total net change in cash, not an FX effect); if no FX-effect-specific tag exists, leave null",
    "capx":   "Capital Expenditures (payments to acquire PP&E)",
    "dvc":    "Common Dividends Paid",
    "dvp":    "Preferred Dividends Paid",
    "prstkc": "Common Stock Repurchases — CASH FLOW STATEMENT only: use PaymentsForRepurchaseOfCommonStock. Do NOT use equity statement tags like StockRepurchasedDuringPeriodValue or TreasuryStockValueAcquiredCostMethod",
    "scstkc": "Proceeds from Issuance of Common Stock",
    "dltis":  "Proceeds from Issuance of Long-term Debt",
    "dltr":   "Repayments of Long-term Debt",
}

# Accounting identities shown to the LLM as reasoning constraints
IDENTITY_PROMPT = """
Accounting identities to guide your tag selection (all values in same currency units):
  1. at = lt + seq                              (Total Assets = Total Liabilities + Total Equity)
  2. gp = sale - cogs                           (Gross Profit = Revenue - Cost of Revenue)
  3. ib ≈ pi - txt - mib_ni                    (Net Income ≈ Pre-tax Income - Tax - NCI income)
  4. oancf + ivncf + fincf + exre ≈ change in cash balance (Cash Flow Statement)
  5. seq = ceq + pstk + mib                    (Total Equity = Common + Preferred + NCI book value)
  6. ni = ib - mib_ni                          (Parent Net Income = Total NI - NCI income)
  7. sale = revt_interest + revt_noninterest   (Bank Total Revenue; banks only)

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


def fetch_xbrl_facts(cik: str, accession: str, form_type: str = "") -> dict[str, float]:
    """Returns {us-gaap concept: value} for all monetary USD facts in this filing.

    For 10-Q filings, duration-based facts (income statement, cash flow) prefer
    the longest-period entry (YTD cumulative) over shorter QTD-only entries.
    """
    from datetime import date as _date

    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    data = _http_get_json(url)

    accession_norm = accession.replace("-", "")
    is_quarterly = form_type.upper().startswith("10-Q")

    # For each concept, track (value, duration_days) — pick the longest-duration match
    best: dict[str, tuple[float, int]] = {}
    us_gaap = data.get("facts", {}).get("us-gaap", {})

    for concept, info in us_gaap.items():
        usd_entries = info.get("units", {}).get("USD", [])
        for entry in usd_entries:
            if entry.get("accn", "").replace("-", "") != accession_norm:
                continue
            val = entry.get("val")
            if val is None:
                continue
            val = float(val)

            start = entry.get("start", "")
            end_str = entry.get("end", "")

            if is_quarterly and start and end_str:
                # Duration fact: compute period length to prefer QTD over YTD.
                # Compustat fundq stores standalone-quarter values (~90 days),
                # not YTD cumulative (~270 days).
                try:
                    duration = (_date.fromisoformat(end_str) - _date.fromisoformat(start)).days
                except Exception:
                    duration = 9999
            else:
                # Instant fact (balance sheet): prefer the LATEST end date
                # (current-quarter balance sheet, not comparative prior-period).
                # Negative ordinal ensures more-recent dates sort as "shorter".
                try:
                    duration = -_date.fromisoformat(end_str).toordinal() if end_str else 0
                except Exception:
                    duration = 0

            existing = best.get(concept)
            if existing is None or duration < existing[1]:
                best[concept] = (val, duration)

    facts = {concept: val for concept, (val, _) in best.items()}
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
- Do NOT assign a composite/parent tag to one field if a component of that composite is already assigned to another field (e.g. if AccountsPayableCurrent is assigned to ap, do not also assign AccountsPayableAndOtherAccruedLiabilitiesCurrent to aco or xacc — pick the more specific standalone tag instead).
- For cash flow fields (oancf, ivncf, fincf, capx, prstkc, scstkc, dvc, dvp, dltis, dltr), only use tags that appear in the cash flow statement — never use equity statement or balance sheet tags.
- NEVER assign CashCashEquivalentsRestrictedCash*PeriodIncreaseDecreaseIncludingExchangeRateEffect to any field — it is the total net change in cash (a derived sum), not a standalone line item.
- Return ONLY a valid JSON object on a single line. No explanation, no markdown, no code block.

Example format:
{{"at": "Assets", "sale": "Revenues", "ib": "NetIncomeLoss", "oancf": null}}

JSON mapping:"""


# ---------------------------------------------------------------------------
# Bedrock invocation
# ---------------------------------------------------------------------------
_BEDROCK_MODEL_FAST   = "us.amazon.nova-lite-v1:0"
_BEDROCK_MODEL_STRONG = "us.deepseek.r1-v1:0"

_MODEL_INFERENCE_CONFIG = {
    _BEDROCK_MODEL_FAST:   {"maxTokens": 2048,  "temperature": 0},
    _BEDROCK_MODEL_STRONG: {"maxTokens": 16000, "temperature": 0},
}

# Residual names that are absolute values (not relative %) — excluded from fallback threshold
_ABSOLUTE_RESIDUAL_CHECKS = {"CashFlow_componentSum", "Bank_NetInterestIncome"}

# If the fast model's max relative residual exceeds this, fall back to the strong model
_FALLBACK_THRESHOLD = 0.15


def _max_relative_residual(residuals: dict) -> float:
    vals = [v for k, v in residuals.items()
            if k not in _ABSOLUTE_RESIDUAL_CHECKS and isinstance(v, float)]
    return max(vals, default=0.0)


def invoke_bedrock(prompt: str, bedrock_client, model_id: str = _BEDROCK_MODEL_STRONG) -> dict[str, Optional[str]]:
    """Calls Bedrock and returns the parsed {field: xbrl_tag_or_null} mapping."""
    inference_config = _MODEL_INFERENCE_CONFIG.get(model_id, {"maxTokens": 4096, "temperature": 0})
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=inference_config,
    )
    # R1 returns a reasoningContent block before the text block — find the text block
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

    # Derive lt = at - seq when Liabilities is not directly tagged in XBRL.
    if "lt" not in values:
        at  = values.get("at",  0.0)
        seq = values.get("seq", 0.0)
        if at and seq:
            values["lt"] = at - seq

    # Derive lt_noncurrent = lt - lct if AI didn't assign it directly.
    # Most companies don't tag a total noncurrent liabilities line in XBRL.
    if "lt_noncurrent" not in values:
        lt  = values.get("lt",  0.0)
        lct = values.get("lct", 0.0)
        if lt and lct:
            values["lt_noncurrent"] = lt - lct

    # Derive ceq = seq - pstk for companies with preferred stock.
    # Fires when: (a) AI left ceq null, or (b) AI mapped ceq to same tag as seq.
    # In both cases, common equity = total equity minus preferred stock carrying value.
    seq_v  = values.get("seq")
    pstk_v = values.get("pstk", 0.0)
    ceq_v  = values.get("ceq")
    if seq_v is not None and pstk_v:
        if ceq_v is None or ceq_v == seq_v:
            values["ceq"] = seq_v - pstk_v

    return values


def _pct_err(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if actual == 0 else float("inf")
    return abs(actual - expected) / abs(expected)


def validate_anchors(values: dict[str, float], industry: str = "GENERAL") -> dict[str, float]:
    """Returns {anchor_name: relative_residual}. 0 = perfect, >0.05 = suspect."""
    residuals: dict[str, float] = {}

    def _check(name: str, actual: float, expected: float) -> None:
        if actual and expected:
            residuals[name] = _pct_err(actual, expected)

    # --- Balance Sheet identity (all industries) ---
    at  = values.get("at",  0.0)
    lt  = values.get("lt",  0.0)
    seq = values.get("seq", 0.0)
    _check("BalanceSheet", at, lt + seq)

    # --- Equity decomposition: seq = ceq + pstk + mib (all industries) ---
    ceq  = values.get("ceq",  0.0)
    pstk = values.get("pstk", 0.0)
    mib  = values.get("mib",  0.0)
    _check("Equity_decomposition", seq, ceq + pstk + mib)

    # --- Liabilities = Current + Noncurrent (not BANK/INSURANCE/FINANCIAL) ---
    if industry not in ("BANK", "INSURANCE", "FINANCIAL"):
        lct          = values.get("lct",          0.0)
        lt_noncurrent = values.get("lt_noncurrent", 0.0)
        _check("Liabilities_total", lt, lct + lt_noncurrent)

    # --- Current Assets partition (not BANK/INSURANCE/FINANCIAL) ---
    if industry not in ("BANK", "INSURANCE", "FINANCIAL"):
        act = values.get("act", 0.0)
        che = values.get("che", 0.0)
        rect = values.get("rect", 0.0)
        invt = values.get("invt", 0.0)
        ivst = values.get("ivst", 0.0)
        xpp  = values.get("xpp",  0.0)
        aco  = values.get("aco",  0.0)
        partition_sum = che + rect + invt + ivst + xpp + aco
        _check("AssetsCurrent_partition", act, partition_sum)

    # --- Current Liabilities partition (not BANK) ---
    if industry != "BANK":
        lct  = values.get("lct",  0.0)
        ap   = values.get("ap",   0.0)
        dlc  = values.get("dlc",  0.0)
        drc  = values.get("drc",  0.0)
        txp  = values.get("txp",  0.0)
        xacc = values.get("xacc", 0.0)
        lco  = values.get("lco",  0.0)
        _check("LiabilitiesCurrent_partition", lct, ap + dlc + drc + txp + xacc + lco)

    # --- Gross Profit identity ---
    sale = values.get("sale", 0.0)
    cogs = values.get("cogs", 0.0)
    gp   = values.get("gp",   0.0)
    _check("GrossProfit", gp, sale - cogs)

    # --- Net Income identity ---
    pi     = values.get("pi",     0.0)
    txt    = values.get("txt",    0.0)
    mib_ni = values.get("mib_ni", 0.0)
    ib     = values.get("ib",     0.0)
    _check("NetIncome", ib, pi - txt - mib_ni)

    # --- Net Income attribution: ni = ib - mib_ni ---
    # Skip for companies with preferred stock: the ni-ib gap may reflect
    # non-cash preferred accretion (not captured in any mapped field), which
    # would produce a large false residual.
    ni   = values.get("ni",   0.0)
    pstk = values.get("pstk", 0.0)
    if not pstk:
        _check("NetIncome_attribution", ni, ib - mib_ni)

    # --- Cash Flow total (reported as sum, not residual) ---
    oancf = values.get("oancf", 0.0)
    ivncf = values.get("ivncf", 0.0)
    fincf = values.get("fincf", 0.0)
    exre  = values.get("exre",  0.0)
    if oancf or ivncf or fincf:
        residuals["CashFlow_componentSum"] = oancf + ivncf + fincf + exre

    # --- Bank: revenue identities ---
    if industry == "BANK":
        revt_interest    = values.get("revt_interest",    0.0)
        revt_noninterest = values.get("revt_noninterest", 0.0)
        xint             = values.get("xint",             0.0)
        sale_v           = values.get("sale",             0.0)
        if revt_interest or xint:
            residuals["Bank_NetInterestIncome"] = revt_interest - xint
        _check("Bank_Revenue_partition", sale_v, revt_interest + revt_noninterest)

    # --- Insurance: Net Premiums = Direct + Assumed - Ceded ---
    if industry == "INSURANCE":
        prem_net     = values.get("prem_net",     0.0)
        prem_gross   = values.get("prem_gross",   0.0)
        prem_assumed = values.get("prem_assumed", 0.0)
        prem_ceded   = values.get("prem_ceded",   0.0)
        _check("Insurance_NetPremiums", prem_net, prem_gross + prem_assumed - prem_ceded)

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
    facts = fetch_xbrl_facts(cik_padded, accession, form_type)
    if not facts:
        raise RuntimeError(f"No XBRL facts found for {cik} / {accession}")

    # 3. Industry
    industry = classify_industry(sic)

    # 4. AI mapping — fast model first, fall back to strong model if residuals are high
    prompt = build_prompt(facts, form_type, industry)
    mapping = invoke_bedrock(prompt, bedrock_client, model_id=_BEDROCK_MODEL_FAST)
    log.info("Fast model (%s) produced %d field assignments", _BEDROCK_MODEL_FAST, sum(1 for v in mapping.values() if v))

    # 5. Extract numeric values
    values = extract_values(facts, mapping)

    # 6. Validate
    residuals = validate_anchors(values, industry)
    max_residual = _max_relative_residual(residuals)
    log.info("Fast model residuals (max=%.3f): %s", max_residual,
             {k: f"{v:.3f}" for k, v in residuals.items() if isinstance(v, float)})

    model_used = _BEDROCK_MODEL_FAST
    if max_residual > _FALLBACK_THRESHOLD:
        log.info("Residual %.3f > %.2f — falling back to %s", max_residual, _FALLBACK_THRESHOLD, _BEDROCK_MODEL_STRONG)
        mapping   = invoke_bedrock(prompt, bedrock_client, model_id=_BEDROCK_MODEL_STRONG)
        values    = extract_values(facts, mapping)
        residuals = validate_anchors(values, industry)
        model_used = _BEDROCK_MODEL_STRONG
        log.info("Strong model residuals: %s", {k: f"{v:.3f}" for k, v in residuals.items() if isinstance(v, float)})

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
    row["_model_used"]       = model_used

    return row
