"""Edgar AI Aggregator Lambda.

Triggered nightly by EventBridge (08:00 UTC) after the edgar_ai_worker run.

Reads individual filing parquets from:
    data-ingress/filings/annual/{cik}/{date}.parquet
    data-ingress/filings/quarterly/{cik}/{date}.parquet

Appends new rows to existing consolidated panels and writes:
    pyData/Intermediate/a_aCompustat.parquet   — annual panel (cik × datadate)
    pyData/Intermediate/m_aCompustat.parquet   — monthly-expanded annual
    pyData/Intermediate/m_QCompustat.parquet   — monthly-expanded quarterly

Incremental: a watermark at data-ingress/filings/.last_aggregated
tracks the last successful run so only new parquets are downloaded.
"""
from __future__ import annotations

import io
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET     = os.environ["S3_BUCKET"]
PYDATA_PREFIX = os.environ.get("PYDATA_PREFIX", "pyData/Intermediate")
WATERMARK_KEY = "data-ingress/filings/.last_aggregated"

_s3 = boto3.client("s3")

# ---------------------------------------------------------------------------
# Field rename: edgar_ai_worker field names → quarterly Compustat names
# ---------------------------------------------------------------------------
_ANNUAL_TO_QUARTERLY: dict[str, str] = {
    "at": "atq", "act": "actq", "che": "cheq", "rect": "rectq",
    "invt": "invtq", "ivst": "ivstq", "xpp": "xppq", "aco": "acoq",
    "ppent": "ppentq", "ppent_rou": "ppentq_rou", "intan": "intanq",
    "gdwl": "gdwlq", "ivao": "ivaoq",
    "lt": "ltq", "lct": "lctq", "lt_noncurrent": "ltq_noncurrent",
    "dlc": "dlcq", "dltt": "dlttq", "ap": "apq", "txp": "txpq",
    "drc": "drcq", "xacc": "xaccq", "lco": "lcoq", "lo": "loq",
    "seq": "seqq", "ceq": "ceqq", "pstk": "pstkq", "re": "req", "mib": "mibq",
    "sale": "saleq", "revt_interest": "revtq_interest",
    "revt_noninterest": "revtq_noninterest",
    "cogs": "cogsq", "gp": "gpq", "xsga": "xsgaq", "xrd": "xrdq",
    "xad": "xadq", "xint": "xintq", "dp": "dpq", "oiadp": "oiadpq",
    "oibdp": "oibdpq", "nopi": "nopiq", "pi": "piq", "txt": "txtq",
    "mib_ni": "mibq_ni", "ib": "ibq", "do": "doq", "xi": "xiq", "ni": "niq",
    # Cash flow fields — YTD suffix convention
    "oancf": "oancfy", "ivncf": "ivncfy", "fincf": "fincfy", "exre": "exreq",
    "capx": "capxy", "dvc": "dvcq", "dvp": "dvpq", "prstkc": "prstkcy",
    "scstkc": "sstky", "dltis": "dltisq", "dltr": "dltrq",
    "datadate": "datadateq",
}

_DIAG_COLS = {"_anchor_residuals", "_ai_mapping", "_model_used", "_xbrl_status"}

# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _read_watermark() -> datetime:
    try:
        obj = _s3.get_object(Bucket=S3_BUCKET, Key=WATERMARK_KEY)
        ts = obj["Body"].read().decode().strip()
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        raise


def _write_watermark(ts: datetime) -> None:
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=WATERMARK_KEY,
        Body=ts.isoformat().encode(),
        ContentType="text/plain",
    )


def _list_new_parquets(prefix: str, watermark: datetime) -> list[dict]:
    """Return S3 object dicts for parquets under prefix modified after watermark."""
    results = []
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            if obj["LastModified"].replace(tzinfo=timezone.utc) <= watermark:
                continue
            results.append({"key": key, "last_modified": obj["LastModified"]})
    return results


def _read_parquet_s3(key: str) -> pd.DataFrame | None:
    """Download one parquet from S3, skip no_facts stubs."""
    try:
        head = _s3.head_object(Bucket=S3_BUCKET, Key=key)
        if head.get("Metadata", {}).get("xbrl_status") == "no_facts":
            return None
        obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
        return pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as exc:
        log.warning("Failed to read %s: %s", key, exc)
        return None


def _load_panel(key: str) -> pd.DataFrame:
    """Load an existing panel parquet from S3, return empty DataFrame if absent."""
    try:
        obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
        return pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return pd.DataFrame()
        raise


def _write_panel(df: pd.DataFrame, key: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=buf.read(),
        ContentType="application/octet-stream",
    )
    log.info("Wrote %d rows to s3://%s/%s", len(df), S3_BUCKET, key)


# ---------------------------------------------------------------------------
# Identifier helpers (inlined from DataIngressModel/utils/ap_identifiers.py)
# ---------------------------------------------------------------------------

def _load_ticker_map() -> pd.DataFrame:
    key = f"{PYDATA_PREFIX}/ticker_to_permno_monthly.csv"
    try:
        obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
        df = pd.read_csv(io.BytesIO(obj["Body"].read()))
        df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
        df["permno"] = pd.to_numeric(df["permno"], errors="coerce").astype("Int64")
        df["permco"] = pd.to_numeric(df.get("permco", df["permno"]), errors="coerce").astype("Int64")
        return df.dropna(subset=["ticker", "permno"]).drop_duplicates("ticker", keep="first")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return pd.DataFrame(columns=["ticker", "permno", "permco"])
        raise


def _save_ticker_map(df: pd.DataFrame) -> None:
    key = f"{PYDATA_PREFIX}/ticker_to_permno_monthly.csv"
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read(), ContentType="text/csv")


def _ensure_permno(df: pd.DataFrame, ticker_map: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach permno/permco, allocating new stable IDs for unseen tickers."""
    tickers = set(df["ticker"].astype(str).str.upper().str.strip().unique())
    existing_tickers = set(ticker_map["ticker"]) if not ticker_map.empty else set()
    new_tickers = sorted(tickers - existing_tickers)

    if new_tickers:
        next_id = (int(ticker_map["permno"].max()) + 1) if not ticker_map.empty else 1
        new_rows = pd.DataFrame({
            "ticker": new_tickers,
            "permno": range(next_id, next_id + len(new_tickers)),
        })
        new_rows["permco"] = new_rows["permno"]
        new_rows["permno"] = new_rows["permno"].astype("Int64")
        new_rows["permco"] = new_rows["permco"].astype("Int64")
        ticker_map = pd.concat([ticker_map, new_rows], ignore_index=True)

    t_map = ticker_map.set_index("ticker")
    norm = df["ticker"].astype(str).str.upper().str.strip()
    df = df.copy()
    df["permno"] = norm.map(t_map["permno"]).astype("Int64")
    df["permco"] = norm.map(t_map["permco"]).astype("Int64")
    df["gvkey"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")
    return df, ticker_map


# ---------------------------------------------------------------------------
# Panel construction helpers
# ---------------------------------------------------------------------------

def _drop_diag(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in _DIAG_COLS if c in df.columns])


def _to_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _derive_annual(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["cik", "datadate"])
    df["wcap"] = df["act"].sub(df["lct"])
    df["debt"] = df["dltt"].add(df["dlc"])
    return df


def _derive_quarterly(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["cik", "datadateq"])
    grp = df.groupby("cik", sort=False)
    df["wcapq"]  = df["actq"].sub(df["lctq"])
    df["chechq"] = grp["cheq"].diff()

    # Use lagged assets/equity for denominator to avoid look-ahead
    lag_atq  = grp["atq"].shift(1)
    lag_seqq = grp["seqq"].shift(1)
    df["roaq"] = df["niq"] / ((df["atq"] + lag_atq) / 2)
    df["roeq"] = df["niq"] / ((df["seqq"] + lag_seqq) / 2)

    # Clip extreme ratios
    for col in ("roaq", "roeq"):
        df[col] = df[col].clip(-10, 10)
    return df


def _monthly_expand(df: pd.DataFrame, date_col: str, lag_months: int, n_months: int) -> pd.DataFrame:
    """Fan each row out to n_months successive months starting at date + lag_months."""
    records = []
    for _, row in df.iterrows():
        base = pd.to_datetime(row[date_col])
        if pd.isna(base):
            continue
        base_period = base.to_period("M") + lag_months
        for offset in range(n_months):
            r = row.copy()
            r["time_avail_m"] = (base_period + offset).to_timestamp()
            records.append(r)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    force_rebuild = event.get("force_rebuild", False)
    watermark = datetime(1970, 1, 1, tzinfo=timezone.utc) if force_rebuild else _read_watermark()
    run_start = datetime.now(timezone.utc)

    log.info("Watermark: %s  force_rebuild=%s", watermark.isoformat(), force_rebuild)

    # 1. Discover new parquets
    new_annual_keys   = _list_new_parquets("data-ingress/filings/annual/",   watermark)
    new_quarterly_keys = _list_new_parquets("data-ingress/filings/quarterly/", watermark)
    log.info("New filings: %d annual, %d quarterly", len(new_annual_keys), len(new_quarterly_keys))

    if not new_annual_keys and not new_quarterly_keys:
        log.info("Nothing new since watermark — exiting")
        return {"status": "no_new_filings"}

    # 2. Download new parquets in parallel
    def _fetch_batch(keys: list[dict]) -> pd.DataFrame:
        frames = []
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(_read_parquet_s3, k["key"]): k for k in keys}
            for fut in as_completed(futs):
                df = fut.result()
                if df is not None and not df.empty:
                    frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    new_annual    = _fetch_batch(new_annual_keys)
    new_quarterly = _fetch_batch(new_quarterly_keys)

    # 3. Load existing panels
    existing_annual    = _load_panel(f"{PYDATA_PREFIX}/a_aCompustat.parquet")
    existing_quarterly = _load_panel(f"{PYDATA_PREFIX}/m_QCompustat.parquet")  # raw quarterly (pre-monthly)

    # 4. Build annual panel
    annual_added = 0
    if not new_annual.empty:
        new_annual = _drop_diag(new_annual)
        new_annual["datadate"] = _to_datetime(new_annual["datadate"])
        new_annual["cik"] = new_annual["cik"].astype(str)

        annual = pd.concat([existing_annual, new_annual], ignore_index=True)
        annual = annual.sort_values(["cik", "datadate"])
        annual = annual.drop_duplicates(subset=["cik", "datadate"], keep="last")
        annual = _derive_annual(annual)
        annual_added = len(new_annual)
    else:
        annual = existing_annual

    # 5. Build quarterly panel
    quarterly_added = 0
    if not new_quarterly.empty:
        new_quarterly = _drop_diag(new_quarterly)
        new_quarterly = new_quarterly.rename(columns=_ANNUAL_TO_QUARTERLY)
        new_quarterly["datadateq"] = _to_datetime(new_quarterly["datadateq"])
        new_quarterly["cik"] = new_quarterly["cik"].astype(str)

        quarterly = pd.concat([existing_quarterly, new_quarterly], ignore_index=True)
        quarterly = quarterly.sort_values(["cik", "datadateq"])
        quarterly = quarterly.drop_duplicates(subset=["cik", "datadateq"], keep="last")
        quarterly = _derive_quarterly(quarterly)
        quarterly_added = len(new_quarterly)
    else:
        quarterly = existing_quarterly

    # 6. Attach permno/gvkey
    ticker_map = _load_ticker_map()

    if not annual.empty and "ticker" in annual.columns:
        annual, ticker_map = _ensure_permno(annual, ticker_map)
    if not quarterly.empty and "ticker" in quarterly.columns:
        quarterly, ticker_map = _ensure_permno(quarterly, ticker_map)

    _save_ticker_map(ticker_map)

    # 7. Write raw panels
    if not annual.empty:
        _write_panel(annual, f"{PYDATA_PREFIX}/a_aCompustat.parquet")

    if not quarterly.empty:
        # Save pre-monthly quarterly as well (used by some predictors directly)
        _write_panel(quarterly, f"{PYDATA_PREFIX}/a_QCompustat.parquet")

    # 8. Monthly fan-out — annual (12 months, +6 month lag)
    if not annual.empty:
        monthly_annual = _monthly_expand(annual, "datadate", lag_months=6, n_months=12)
        if not monthly_annual.empty:
            monthly_annual = monthly_annual.sort_values(["cik", "time_avail_m", "datadate"])
            monthly_annual = monthly_annual.drop_duplicates(subset=["cik", "time_avail_m"], keep="last")
            _write_panel(monthly_annual, f"{PYDATA_PREFIX}/m_aCompustat.parquet")

    # 9. Monthly fan-out — quarterly (3 months, +3 month lag)
    if not quarterly.empty:
        monthly_quarterly = _monthly_expand(quarterly, "datadateq", lag_months=3, n_months=3)
        if not monthly_quarterly.empty:
            monthly_quarterly = monthly_quarterly.sort_values(["cik", "time_avail_m", "datadateq"])
            monthly_quarterly = monthly_quarterly.drop_duplicates(subset=["cik", "time_avail_m"], keep="last")
            _write_panel(monthly_quarterly, f"{PYDATA_PREFIX}/m_QCompustat.parquet")

    # 10. Update watermark
    _write_watermark(run_start)

    result = {
        "status":           "ok",
        "annual_added":     annual_added,
        "quarterly_added":  quarterly_added,
        "annual_total":     len(annual),
        "quarterly_total":  len(quarterly),
    }
    log.info("Done: %s", json.dumps(result))
    return result
