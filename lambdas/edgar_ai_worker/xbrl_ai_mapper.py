"""XBRL-to-Compustat field mapper using Kimi K2.6 via OpenRouter + accounting identity validation.

Entry point: map_filing(cik, accession, form_type, ticker, sic, openrouter_api_key)
Returns a flat dict of Compustat field values for one filing, ready to write as parquet.

Uses the SEC EDGAR companyfacts REST API (no edgartools dependency) so it is
lightweight enough to run inside a Lambda function.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

EDGAR_IDENTITY = os.environ.get("EDGAR_IDENTITY", "EuclideanResearch podreze03@gmail.com")

# ---------------------------------------------------------------------------
# Compustat field catalogue fed to the LLM
# ---------------------------------------------------------------------------
COMPUSTAT_FIELDS: dict[str, str] = {
    # Balance Sheet — Assets
    "at":        "Total Assets",
    "act":       "Total Current Assets",
    "che":       "Cash and Equivalents (CashAndCashEquivalentsAtCarryingValue or post-2018 CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents)",
    "rect":      "Accounts Receivable Net (banks: net loans)",
    "invt":      "Inventory Net",
    "ivst":      "Short-term Investments / Marketable Securities",
    "xpp":       "Prepaid Expenses (current)",
    "aco":       "Other Current Assets",
    "ppent":     "Property Plant & Equipment NET (PropertyPlantAndEquipmentNet; excludes ROU)",
    "ppent_rou": "Operating Lease Right-of-Use Assets (ASC 842)",
    "intan":     "Intangible Assets Net (excluding goodwill)",
    "gdwl":      "Goodwill",
    "ivao":      "Long-term Investments",
    # Balance Sheet — Liabilities
    "lt":            "Total Liabilities (Liabilities tag only; null if absent)",
    "lct":           "Total Current Liabilities",
    "lt_noncurrent": "Total Noncurrent Liabilities",
    "dlc":           "Short-term Debt (current maturities + ST borrowings)",
    "dltt":          "Long-term Debt Noncurrent",
    "ap":            "Accounts Payable (trade)",
    "txp":           "Income Taxes Payable",
    "drc":           "Deferred Revenue (current)",
    "xacc":          "Accrued Liabilities (AccruedLiabilitiesCurrent or composite fallback)",
    "lco":           "Other Current Liabilities (OtherLiabilitiesCurrent, OperatingLeaseLiabilityCurrent)",
    "lo":            "Other Noncurrent Liabilities",
    # Balance Sheet — Equity
    "seq":  "Total Stockholders Equity INCLUDING NCI (StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest)",
    "ceq":  "Common Equity attributable to parent",
    "pstk": "Preferred Stock carrying value. Use PreferredStockValue or PreferredStockValueOutstanding. NEVER TemporaryEquity* (that is mezzanine/redeemable preferred — separate from pstk). NEVER RedeemableNoncontrollingInterest* (that is mib).",
    "re":   "Retained Earnings (Accumulated Deficit)",
    "mib":  "Noncontrolling Interest balance (null for LP structures using PartnersCapital)",
    # Income Statement
    "sale":          "Net Revenue / Net Sales (banks: net interest + noninterest income)",
    "revt_interest": "Interest Income (banks only)",
    "revt_noninterest": "Non-Interest Income (banks only)",
    "cogs":   "Cost of Goods Sold / Cost of Revenue",
    "gp":     "Gross Profit",
    "xsga":   "Selling, General & Administrative Expense",
    "xrd":    "Research & Development Expense",
    "xad":    "Advertising Expense",
    "xint":   "Interest Expense",
    "dp":     "Depreciation & Amortization (prefer DepreciationDepletionAndAmortization)",
    "oiadp":  "Operating Income / Loss (AFTER D&A)",
    "oibdp":  "Operating Income BEFORE D&A (EBITDA; often not directly tagged)",
    "nopi":   "Non-operating Income net — includes gains/losses on asset sales",
    "pi":     "Pre-tax Income from Continuing Operations",
    "txt":    "Income Tax Expense (Benefit) from Continuing Operations",
    "mib_ni": "Net Income attributable to NCI",
    "ib":     "Income from Continuing Operations net of tax. Prefer IncomeLossFromContinuingOperations (AFTER-TAX). If absent and company has no NCI or discontinued ops, NetIncomeLoss is acceptable. FORBIDDEN: any pre-tax tag (IncomeLossFromContinuingOperationsBeforeIncomeTaxes*), IncomeLossAttributableToParent, NetIncomeLossAvailableToCommonShareholders*.",
    "do":     "Discontinued Operations net of tax (IncomeLossFromDiscontinuedOperationsNetOfTax)",
    "xi":     "Extraordinary Items net of tax (rare post-2015)",
    "ni":     "Net Income — total consolidated (ni = ib + do + xi)",
    # Cash Flow — outflow fields stored as POSITIVE magnitudes
    "oancf":  "Net Cash from Operating Activities (NetCashProvidedByUsedInOperatingActivities). If no aggregate tag exists, use a LIST: [NetCashProvidedByUsedInOperatingActivitiesContinuingOperations, CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations]",
    "ivncf":  "Net Cash from Investing Activities",
    "fincf":  "Net Cash from Financing Activities",
    "exre":   "Effect of FX on Cash (EffectOfExchangeRateOnCash* only)",
    "capx":   "Capital Expenditures (PaymentsToAcquirePropertyPlantAndEquipment)",
    "dvc":    "Common Dividends Paid",
    "dvp":    "Preferred Dividends Paid",
    "prstkc": "Common Stock Repurchases (PaymentsForRepurchaseOfCommonStock; cash flow only)",
    "scstkc": "Proceeds from Issuance of Common Stock",
    "dltis":  "Proceeds from Issuance of Long-term Debt",
    "dltr":   "Repayments of Long-term Debt",
}

# Accounting identities shown to the LLM as context — not targets to satisfy
IDENTITY_PROMPT = """
These accounting identities will naturally hold when tags are mapped correctly.
Do NOT choose a tag just to satisfy an identity — if no clearly matching tag exists, return null.
  1. at = lt + seq                              (Total Assets = Total Liabilities + Total Equity)
  2. gp = sale - cogs                           (Gross Profit = Revenue - Cost of Revenue)
  3. ib = pi - txt                              (Continuing ops net income = Pre-tax Income - Tax)
  4. ni = ib + do + xi                          (Total Net Income = Continuing ops + Discontinued ops + Extraordinary items)
  5. oibdp = oiadp + dp                         (EBITDA = Operating Income + D&A)
  6. seq = ceq + pstk                           (Total Equity = Common Equity + Preferred Stock)
  7. oancf + ivncf + fincf + exre ≈ change in cash balance (Cash Flow Statement)
  8. sale = revt_interest + revt_noninterest    (Bank Total Revenue; banks only)
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

    # For each concept, track (value, duration_days) — pick the longest-duration match.
    # Check both us-gaap and ifrs-full taxonomies; IFRS filers (e.g. Canadian companies
    # on NYSE) use ifrs-full tags which Kimi maps to Compustat fields just as naturally.
    best: dict[str, tuple[float, int]] = {}
    facts_section = data.get("facts", {})
    taxonomy = facts_section.get("us-gaap") or facts_section.get("ifrs-full") or {}
    if not taxonomy:
        log.warning("No us-gaap or ifrs-full facts found for accession %s", accession)

    for concept, info in taxonomy.items():
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


def build_prompt(tag_values: dict[str, float], form_type: str, industry: str, sic: Optional[str] = None) -> str:
    # Sort tags by absolute value descending so the most material items appear first
    sorted_tags = sorted(tag_values.items(), key=lambda x: abs(x[1]), reverse=True)

    tag_lines = "\n".join(
        f"  {concept}: {_fmt_value(val)}" for concept, val in sorted_tags
    )

    field_lines = "\n".join(
        f"  {field}: {desc}" for field, desc in COMPUSTAT_FIELDS.items()
    )

    sic_label = f"SIC {sic}" if sic else ""
    sic_int = int(sic) if sic and sic.strip().isdigit() else 0
    if industry in ("BANK", "FINANCIAL"):
        industry_note = (
            f"\nNote: {sic_label} ({industry}). "
            "sale = NET revenue = net interest income + noninterest income (NOT gross interest income). "
            "Always map sale — it equals revt_interest - xint + revt_noninterest. "
            "If no aggregate tag exists, use a LIST: [InterestAndDividendIncomeOperating (or equivalent), NoninterestIncome] and subtract xint via the identity. "
            "Prefer tags like RevenuesNetOfInterestExpense, RevenueFromContractWithCustomerExcludingAssessedTax, or InterestIncomeExpenseNet + NoninterestIncome if present. "
            "rect = net loans and leases receivable. "
            "cogs, invt, gp, and xsga should be null — banks have no inventory or cost of goods sold. "
        )
    elif industry == "UTILITY":
        industry_note = (
            f"\nNote: {sic_label} (UTILITY). "
            "sale = operating revenues (use RevenuesNet, ElectricUtilityRevenue, or UtilityRevenue tags). "
            "dp includes depletion for gas utilities. "
        )
    elif 1300 <= sic_int <= 1399:
        industry_note = (
            f"\nNote: {sic_label} (Oil & Gas). "
            "dp includes depletion and amortization of oil/gas properties (DepletionDepreciationAndAmortization). "
            "Exploration costs (xrd proxy) may be expensed (successful-efforts) or capitalized. "
            "cogs = lease operating expenses + production taxes. "
        )
    elif industry in ("INSURANCE", "REIT"):
        industry_note = f"\nNote: {sic_label} ({industry})." if sic_label else f"\nNote: {industry}."
    else:
        industry_note = f"\nNote: {sic_label}." if sic_label else ""

    return f"""You are mapping XBRL tags from a SEC {form_type} filing to Compustat (WRDS) fields.
Choose the tag matching Compustat's documented definition — not the closest label match.

XBRL tags in this filing (tag: USD value):
{tag_lines}

Map each field below to ONE tag (or a LIST per rules). Return null if no clear match — never assign to satisfy an identity.
{industry_note}
{IDENTITY_PROMPT}

Fields:
{field_lines}

Rules:
- Prefer specific tags over parent/composite tags.
- Don't map the same tag to two fields (exception: seq and ceq may share an equity tag).
- Don't use a composite if its component is already mapped (e.g. if AccountsPayableCurrent → ap, don't reuse AccountsPayableAndOtherAccruedLiabilitiesCurrent elsewhere).
- Sub-component fields (xpp, aco, lco, ivst, mib, mib_ni, pstk, drc, txp) → null when not separately tagged.
- Cash flow fields (oancf, ivncf, fincf, capx, prstkc, scstkc, dvc, dvp, dltis, dltr) must come from cash flow statement tags only.
- Outflow fields (capx, prstkc, dvc, dltr) are stored as POSITIVE magnitudes (Payments* tags are already positive).
- ppent must be NET (PropertyPlantAndEquipmentNet, not Gross).
- seq INCLUDES NCI — prefer StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest over plain StockholdersEquity when both exist.
- Gains/losses on asset sales and goodwill impairments go to nopi or xsga, never sale.
- A field may map to a LIST of tags when no aggregate exists. Valid only for: xsga, dp, xint, dlc, oancf.

`ib` (Income from Continuing Operations net of tax) MAPPING RULES — read carefully:
  1. FIRST CHOICE: IncomeLossFromContinuingOperations  (this is the AFTER-TAX continuing-ops line)
  2. FALLBACK only if #1 is absent AND company has no NCI AND no discontinued ops: NetIncomeLoss
  3. FORBIDDEN for ib (no exceptions):
     - Any tag containing "BeforeIncomeTaxes" or "BeforeTax" — those are pi (pre-tax), not ib
     - IncomeLossAttributableToParent — that's parent-only, not consolidated
     - NetIncomeLoss*AvailableToCommonStockholders* — that's after preferred dividends, not ib
     - NetIncomeLoss when mib_ni is also being mapped — use IncomeLossFromContinuingOperations instead

When choosing between Revenue tags (Excluding/IncludingAssessedTax), pick the one
where sale - cogs ≈ GrossProfit (if GrossProfit is also tagged in this filing).

Universal FORBIDDEN tags (never use for any field):
  * CashCashEquivalentsRestrictedCash*PeriodIncreaseDecreaseIncludingExchangeRateEffect (derived net change, not a line item)
  * LiabilitiesAndStockholdersEquity (= total assets, not liabilities)
  * StockRepurchasedDuringPeriodValue, TreasuryStockValueAcquiredCostMethod (equity statement, not cash flow)
  * TemporaryEquityCarryingAmount* (mezzanine/redeemable equity — not regular pstk)

Return ONLY a JSON object."""


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
_OPENROUTER_API_URL    = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL_FREE = "moonshotai/kimi-k2.6:free"
_OPENROUTER_MODEL_PAID = "moonshotai/kimi-k2.5"

# Residual threshold — fractional error above this is flagged as suspicious.
_FAIL_THRESHOLD = 0.05

# Fields where the AI is permitted to return a LIST of tags (values are summed).
_MULTI_TAG_FIELDS = {"xsga", "dp", "xint", "dlc", "oancf"}

# Per-field tag denylist — if Kimi maps any of these, drop the mapping silently.
# Used by _sanitize_mapping() before extract_values() to enforce hard rules that
# the prompt repeatedly fails to make stick.
_FIELD_TAG_DENYLIST: dict[str, tuple[str, ...]] = {
    "ib": (
        # Pre-tax tags (these are pi, not ib)
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
        # Parent-only / post-preferred-dividend tags
        "IncomeLossAttributableToParent",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
        "NetIncomeLossFromContinuingOperationsAvailableToCommonShareholdersBasic",
        "NetIncomeLossFromContinuingOperationsAvailableToCommonShareholdersDiluted",
    ),
    "pstk": (
        # Mezzanine / redeemable equity — not regular preferred stock
        "TemporaryEquityCarryingAmount",
        "TemporaryEquityCarryingAmountAttributableToParent",
        "TemporaryEquityCarryingAmountAttributableToNoncontrollingInterest",
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
        "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
    ),
    "seq": (
        # Total assets misidentified as equity
        "LiabilitiesAndStockholdersEquity",
    ),
    "ni": (
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    ),
}

# Priority-ordered XBRL tags for the net change in cash this period.
_DCASH_TAGS = [
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
    "CashAndCashEquivalentsPeriodIncreaseDecrease",
    "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
    "NetCashProvidedByUsedInContinuingOperations",
]


def _parse_llm_json(text: str) -> dict[str, Optional[str | list[str]]]:
    """Extract and normalise the JSON mapping from an LLM response string.

    Values may be a single tag string, a list of tag strings (for sum fields),
    or null. Uses the LAST JSON object in the text — the final answer, not any
    intermediate examples the model may have written during reasoning.
    """
    matches = list(re.finditer(r"\{[^{}]*\}", text, re.DOTALL))
    # Prefer the last large object (the mapping); fall back to full greedy match
    json_match = None
    for m in reversed(matches):
        if len(m.group(0)) > 100:
            json_match = m
            break
    if not json_match:
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        raise ValueError(f"No JSON object found in LLM response: {text[:200]}")
    raw = json.loads(json_match.group(0))
    result: dict[str, Optional[str | list[str]]] = {}
    for field in COMPUSTAT_FIELDS:
        if field not in raw:
            continue
        val = raw[field]
        if not val:
            result[field] = None
        elif isinstance(val, list):
            # LIST only valid for fields that can be summed from components
            if field not in _MULTI_TAG_FIELDS:
                log.warning("Rejecting list mapping for %s (not a multi-tag field): %s", field, val)
                result[field] = None
            else:
                tags = [t for t in val if t]
                result[field] = tags if len(tags) > 1 else (tags[0] if tags else None)
        else:
            result[field] = val
    return result


def _call_openrouter(model: str, prompt: str, api_key: str) -> tuple[str, dict]:
    """Single OpenRouter call. Returns (model_used, parsed_mapping). Raises on HTTP error."""
    request_body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16000,
        "temperature": 0,
        "reasoning": {"enabled": False},
        "response_format": {"type": "json_object"},
    }
    payload = json.dumps(request_body).encode()

    req = urllib.request.Request(
        _OPENROUTER_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/aepodrez/EuclideanInfra",
            "X-Title": "Euclidean XBRL Mapper",
        },
        method="POST",
    )

    response_body: dict = {}
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                response_body = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            if attempt == 3 or exc.code != 429:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 30 * attempt
            try:
                err_body = exc.read().decode(errors="replace")
                log.warning("OpenRouter 429 (%s) attempt %d: %s — retrying in %ds", model, attempt, err_body[:200], wait)
            except Exception:
                log.warning("OpenRouter 429 (%s) attempt %d — retrying in %ds", model, attempt, wait)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 3:
                raise
            wait = 5 * attempt
            log.warning("OpenRouter (%s) attempt %d failed (%s), retrying in %ds", model, attempt, exc, wait)
            time.sleep(wait)

    msg = response_body["choices"][0]["message"]
    reasoning = msg.get("reasoning") or ""
    if reasoning:
        log.info("Kimi reasoning (%d chars): %s", len(reasoning), reasoning[:8000])
    text = (msg.get("content") or reasoning or "").strip()
    return model, _parse_llm_json(text)


def _invoke_kimi(prompt: str, api_key: str) -> tuple[str, dict[str, Optional[str]]]:
    """Try free tier first; fall back to paid if rate-limited or quota exhausted. Returns (model_used, mapping)."""
    try:
        return _call_openrouter(_OPENROUTER_MODEL_FREE, prompt, api_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            log.warning("Free tier rate-limited — falling back to paid kimi-k2.6")
        elif exc.code == 402:
            log.warning("Free tier quota/credits exhausted (402) — falling back to paid kimi-k2.6")
        else:
            raise
    except ValueError:
        log.warning("Free tier returned no JSON — falling back to paid kimi-k2.6")

    # Paid model (non-thinking) — content is always populated.
    for attempt in range(1, 3):
        try:
            return _call_openrouter(_OPENROUTER_MODEL_PAID, prompt, api_key)
        except ValueError:
            if attempt == 2:
                raise
            log.warning("Paid model attempt %d returned no JSON — retrying", attempt)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Value extraction + anchor validation
# ---------------------------------------------------------------------------
def _sanitize_mapping(mapping: dict[str, Optional[str | list[str]]]) -> None:
    """In-place: drop any field→tag pairs that violate the per-field denylist.

    Catches the recurring prompt failures (Kimi mapping pre-tax tags to `ib`,
    mezzanine equity to `pstk`, etc.) before they reach extract_values.
    """
    for field, denied in _FIELD_TAG_DENYLIST.items():
        tag = mapping.get(field)
        if not tag:
            continue
        if isinstance(tag, list):
            cleaned = [t for t in tag if t not in denied]
            if len(cleaned) != len(tag):
                log.warning("Sanitize: dropped denied tags from %s list: %s", field, set(tag)-set(cleaned))
            mapping[field] = cleaned if cleaned else None
        elif tag in denied:
            log.warning("Sanitize: dropped %s mapping for %s (denylisted)", tag, field)
            mapping[field] = None


def extract_values(facts: dict[str, float], mapping: dict[str, Optional[str | list[str]]]) -> dict[str, float]:
    """Apply the AI mapping to get numeric values for each Compustat field."""
    _sanitize_mapping(mapping)

    values: dict[str, float] = {}
    for field, tag in mapping.items():
        if not tag:
            continue
        if isinstance(tag, list):
            total = sum(facts[t] for t in tag if t in facts)
            found = [t for t in tag if t in facts]
            if found:
                values[field] = total
                if len(found) < len(tag):
                    missing = [t for t in tag if t not in facts]
                    log.warning("Multi-tag %s: missing tags %s", field, missing)
        elif tag in facts:
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

    # Derive seq = ceq when sole equity class is common (no preferred stock, no NCI balance).
    # Covers companies where XBRL only tags StockholdersEquity (parent-only) = ceq = seq.
    if values.get("seq") is None and values.get("ceq") is not None:
        if not values.get("pstk") and not values.get("mib"):
            values["seq"] = values["ceq"]

    # Derive ib = pi - txt when ib is missing (often because we dropped a forbidden tag above).
    # This is the GAAP identity and is safe whenever both pi and txt are mapped.
    if values.get("ib") is None and "pi" in values and "txt" in values:
        values["ib"] = values["pi"] - values["txt"]

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


def validate_anchors(values: dict[str, float], industry: str = "GENERAL", facts: dict[str, float] | None = None) -> dict[str, float]:
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

    # --- Liabilities = Current + Noncurrent (not BANK/INSURANCE/FINANCIAL) ---
    if industry not in ("BANK", "INSURANCE", "FINANCIAL"):
        lct          = values.get("lct",          0.0)
        lt_noncurrent = values.get("lt_noncurrent", 0.0)
        _check("Liabilities_total", lt, lct + lt_noncurrent)

    # --- Gross Profit identity ---
    # $1M absolute floor — micro-caps with sub-million GP otherwise produce huge %-residuals.
    sale = values.get("sale", 0.0)
    cogs = values.get("cogs", 0.0)
    gp   = values.get("gp",   0.0)
    if gp and sale and cogs:
        exp = sale - cogs
        if _pct_err(gp, exp) > _FAIL_THRESHOLD and abs(gp - exp) > 1_000_000:
            residuals["GrossProfit"] = _pct_err(gp, exp)

    # --- Net Income identity ---
    # Accept ib = pi - txt (consolidated) OR ib = pi - txt - mib_ni (parent-attributable),
    # whichever has the smaller error. Also require absolute error > $1M to avoid noise
    # from rounding and minor NCI adjustments on small companies.
    pi     = values.get("pi",     0.0)
    txt    = values.get("txt",    0.0)
    mib_ni = values.get("mib_ni", 0.0)
    ib     = values.get("ib",     0.0)
    if ib and (pi or txt):
        exp_consolidated = pi - txt
        exp_parent       = pi - txt - mib_ni
        err_consol  = _pct_err(ib, exp_consolidated)
        err_parent  = _pct_err(ib, exp_parent) if mib_ni else err_consol
        best_pct    = min(err_consol, err_parent)
        best_abs    = min(abs(ib - exp_consolidated), abs(ib - exp_parent))
        if best_pct > _FAIL_THRESHOLD and best_abs > 1_000_000:
            residuals["NetIncome"] = best_pct

    # --- Net Income attribution: ni = ib + discontinued ops + extraordinary items ---
    ni = values.get("ni", 0.0)
    do = values.get("do", 0.0)
    xi = values.get("xi", 0.0)
    if ni and ib:
        exp_ni = ib + do + xi
        if _pct_err(ni, exp_ni) > _FAIL_THRESHOLD and abs(ni - exp_ni) > 1_000_000:
            residuals["NetIncome_attribution"] = _pct_err(ni, exp_ni)

    # --- EBITDA identity: oibdp = oiadp + dp ---
    oiadp = values.get("oiadp", 0.0)
    oibdp = values.get("oibdp", 0.0)
    dp    = values.get("dp",    0.0)
    if oibdp and oiadp and dp:
        _check("EBITDA", oibdp, oiadp + dp)

    # --- Equity decomposition: seq = ceq + pstk ---
    # Require $1M absolute floor — tiny pstk values on small companies otherwise blow up.
    ceq  = values.get("ceq",  0.0)
    pstk = values.get("pstk", 0.0)
    seq  = values.get("seq",  0.0)
    if seq and ceq and pstk:
        exp = ceq + pstk
        if _pct_err(seq, exp) > _FAIL_THRESHOLD and abs(seq - exp) > 1_000_000:
            residuals["Equity_decomposition"] = _pct_err(seq, exp)

    # --- Balance sheet sign sanity ---
    for field in ("che", "rect", "invt", "dltt", "at", "act", "ppent"):
        v = values.get(field)
        if v is not None and v < 0:
            residuals[f"Sign_{field}"] = abs(v)

    # --- Cash Flow total: component sum should equal reported net change in cash ---
    # Threshold raised to 8% with $5M absolute floor — small-cap CF rounding produces
    # large percentage residuals on tiny absolute differences.
    oancf = values.get("oancf", 0.0)
    ivncf = values.get("ivncf", 0.0)
    fincf = values.get("fincf", 0.0)
    exre  = values.get("exre",  0.0)
    if (oancf or ivncf or fincf) and facts:
        dcash = next((facts[t] for t in _DCASH_TAGS if t in facts), None)
        if dcash is not None:
            cf_sum = oancf + ivncf + fincf + exre
            if dcash and cf_sum:
                pct = _pct_err(cf_sum, dcash)
                if pct > 0.08 and abs(cf_sum - dcash) > 5_000_000:
                    residuals["CashFlow_total"] = pct

    # --- Effective tax rate sanity (non-bank) ---
    # txt / pi should be 0–60% for profitable companies with meaningful pre-tax income.
    # Skip companies with tiny pi (<$10M) — small absolute tax adjustments blow up the percentage.
    if industry not in ("BANK", "FINANCIAL"):
        pi_v  = values.get("pi",  0.0)
        txt_v = values.get("txt", 0.0)
        if pi_v and pi_v > 10_000_000 and txt_v:
            etr = txt_v / pi_v
            if not (0.0 <= etr <= 0.60):
                residuals["TaxRate_sanity"] = abs(etr)

    # --- Cash flow sign checks ---
    capx_v = values.get("capx", None)
    if capx_v is not None and capx_v < 0:
        residuals["Sign_capx"] = abs(capx_v)

    # --- Bank: revenue identities ---
    # sale = net interest income + noninterest income
    # net interest income = revt_interest - xint
    if industry == "BANK":
        revt_interest    = values.get("revt_interest",    0.0)
        revt_noninterest = values.get("revt_noninterest", 0.0)
        xint             = values.get("xint",             0.0)
        sale_v           = values.get("sale",             0.0)
        net_interest     = revt_interest - xint
        # Only check net interest income identity when all three revenue components are mapped
        if sale_v and revt_interest and revt_noninterest:
            _check("Bank_NetInterestIncome", net_interest, sale_v - revt_noninterest)
        _check("Bank_Revenue_partition", sale_v, net_interest + revt_noninterest)

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
    openrouter_api_key: str = "",
) -> dict:
    """Fetch XBRL facts, map to Compustat fields via Kimi K2.6, validate, return row dict."""
    cik_padded = str(int(cik)).zfill(10)

    log.info("map_filing: cik=%s accession=%s form=%s ticker=%s", cik, accession, form_type, ticker)

    # 1. Filing metadata
    metadata = fetch_filing_metadata(cik_padded, accession)
    report_date = metadata.get("report_date", "")
    log.info("report_date=%s", report_date)

    # 2. XBRL facts
    facts = fetch_xbrl_facts(cik_padded, accession, form_type)
    if not facts:
        log.warning(
            "No XBRL facts for cik=%s accession=%s — returning no_facts stub (skip Kimi)",
            cik, accession,
        )
        return {
            "cik":               cik_padded,
            "ticker":            ticker,
            "datadate":          report_date,
            "form_type":         form_type,
            "industry":          classify_industry(sic),
            "_xbrl_status":      "no_facts",
            "_anchor_residuals": "{}",
            "_ai_mapping":       "{}",
            "_model_used":       "",
        }

    # 3. Industry
    industry = classify_industry(sic)

    # 4. Map with Kimi K2.6
    prompt = build_prompt(facts, form_type, industry, sic=sic)
    model_used, mapping = _invoke_kimi(prompt, openrouter_api_key)
    log.info("Kimi (%s) produced %d field assignments", model_used, sum(1 for v in mapping.values() if v))

    # 5. Extract numeric values
    values = extract_values(facts, mapping)

    # 6. Validate
    residuals = validate_anchors(values, industry, facts=facts)
    log.info("Residuals: %s", {k: f"{v:.3f}" for k, v in residuals.items() if isinstance(v, float)})

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
