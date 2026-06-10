"""Market-data Lambda dispatcher.

One container image, one Lambda function per script. The function's JOB env var
selects which DataIngressModel/DataDownloads script to run. The handler:

  1. Sets up a writable /tmp workspace and points the script's relative paths at it
     (CWD = /tmp/DataDownloads so "../pyData/Intermediate" -> /tmp/pyData/Intermediate
      and "../Static" -> /tmp/Static).
  2. Downloads universe.csv and (for incremental jobs) the existing output parquet(s)
     from S3 so the scripts' append/dedup logic resumes where it left off.
  3. Imports the script module and runs main() (or just imports it, for scripts whose
     work happens at module top-level, e.g. VIX).
  4. Uploads the produced parquet/csv files back to S3.

Env vars:
  S3_BUCKET      (required)
  JOB            (required) — key into JOB_SPECS
  PYDATA_PREFIX  (default "pyData/Intermediate")
  UNIVERSE_KEY   (default "universe.csv")
  STATIC_PREFIX  (default "Static")
  FRED_SSM_PARAM (default "/euclidean/market-data/fred-api-key")
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET      = os.environ["S3_BUCKET"]
PYDATA_PREFIX  = os.environ.get("PYDATA_PREFIX", "pyData/Intermediate")
UNIVERSE_KEY   = os.environ.get("UNIVERSE_KEY", "universe.csv")
STATIC_PREFIX  = os.environ.get("STATIC_PREFIX", "Static")
FRED_SSM_PARAM = os.environ.get("FRED_SSM_PARAM", "/euclidean/market-data/fred-api-key")

CODE_DIR    = os.environ.get("MD_CODE_DIR", "/var/task/DataDownloads")
UTILS_DIR   = os.environ.get("MD_UTILS_DIR", "/var/task/utils")
WORK_DIR    = "/tmp/DataDownloads"        # CWD at runtime (relative paths resolve from here)
OUTPUT_DIR  = "/tmp/pyData/Intermediate"
STATIC_DIR  = "/tmp/Static"
UNIVERSE_TMP = "/tmp/universe.csv"

_s3  = boto3.client("s3")
_ssm = boto3.client("ssm")

# job -> spec. "module" is the DataDownloads file (no .py). "outputs" are the files the
# script writes (synced to/from S3). "needs_fred" pulls the FRED key from SSM.
# "needs_portfolio" pulls Static/ff3_portfolios.csv (FamaFrench inputs).
JOB_SPECS: dict[str, dict] = {
    "vix":                 {"module": "VIX",               "outputs": ["d_vix.parquet"]},
    "crsp_daily":          {"module": "CRSPDaily",         "outputs": ["dailyCRSP.parquet", "dailyCRSPprc.parquet"]},
    "crsp_monthly":        {"module": "CRSPMonthly",       "outputs": ["monthlyCRSP.parquet"]},
    "crsp_distributions":  {"module": "CRSPDistributions", "outputs": ["CRSPdistributions.parquet"]},
    "fama_french_daily":   {"module": "FamaFrenchDaily",   "outputs": ["dailyFF.parquet"],   "needs_fred": True, "needs_portfolio": True},
    "fama_french_monthly": {"module": "FamaFrenchMonthly", "outputs": ["monthlyFF.parquet"], "needs_fred": True, "needs_portfolio": True},
    "market_returns":      {"module": "MarketReturns",     "outputs": ["monthlyMarket.parquet"]},
    "treasury_bill_3m":    {"module": "TreasuryBill3M",    "outputs": ["TBill3M.parquet"], "needs_fred": True},
    "ipo_dates":           {"module": "IPODates",          "outputs": ["IPODates.parquet", "IPODates.csv"], "entry": "build_ap_ipodates"},
}


def _download(key: str, dest: str) -> bool:
    """Download s3://bucket/key -> dest. Returns False if the object doesn't exist."""
    try:
        _s3.download_file(S3_BUCKET, key, dest)
        log.info("downloaded s3://%s/%s -> %s", S3_BUCKET, key, dest)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "403", "AccessDenied"):
            log.info("no existing object at s3://%s/%s (skipping)", S3_BUCKET, key)
            return False
        raise


def _upload(local_path: str, key: str) -> None:
    _s3.upload_file(local_path, S3_BUCKET, key)
    log.info("uploaded %s -> s3://%s/%s", local_path, S3_BUCKET, key)


def _load_fred_key() -> None:
    """Read FRED_API_KEY from SSM and export it; no-op if unavailable."""
    if os.environ.get("FRED_API_KEY"):
        return
    try:
        resp = _ssm.get_parameter(Name=FRED_SSM_PARAM, WithDecryption=True)
        os.environ["FRED_API_KEY"] = resp["Parameter"]["Value"]
        log.info("loaded FRED_API_KEY from SSM")
    except Exception as e:
        log.warning("could not load FRED_API_KEY from SSM (%s); continuing without it", e)


def lambda_handler(event, context):
    job = (event or {}).get("job") or os.environ.get("JOB")
    if job not in JOB_SPECS:
        raise ValueError(f"Unknown job '{job}'. Valid: {sorted(JOB_SPECS)}")
    spec = JOB_SPECS[job]
    t0 = time.time()
    log.info("=== market-data job: %s (module=%s) ===", job, spec["module"])

    # 1. Writable workspace; relative paths in the scripts resolve from WORK_DIR
    for d in (WORK_DIR, OUTPUT_DIR, STATIC_DIR):
        os.makedirs(d, exist_ok=True)
    os.chdir(WORK_DIR)
    os.environ["AP_OUTPUT_DIR"] = OUTPUT_DIR
    os.environ["UNIVERSE_CSV"]  = UNIVERSE_TMP

    # 2. Universe
    _download(UNIVERSE_KEY, UNIVERSE_TMP)

    # 3. Existing outputs (so incremental append resumes)
    for fname in spec["outputs"]:
        _download(f"{PYDATA_PREFIX}/{fname}", os.path.join(OUTPUT_DIR, fname))

    # 4. Optional inputs
    if spec.get("needs_fred"):
        _load_fred_key()
    if spec.get("needs_portfolio"):
        _download(f"{STATIC_PREFIX}/ff3_portfolios.csv", os.path.join(STATIC_DIR, "ff3_portfolios.csv"))

    # 5. Run the script
    if CODE_DIR not in sys.path:
        sys.path.insert(0, CODE_DIR)
    if UTILS_DIR not in sys.path:
        sys.path.insert(0, UTILS_DIR)
    module = importlib.import_module(spec["module"])
    entry = spec.get("entry", "main")
    if hasattr(module, entry):
        getattr(module, entry)()
    # else: module did its work at import time (e.g. VIX)

    # 6. Upload outputs
    uploaded = []
    for fname in spec["outputs"]:
        local_path = os.path.join(OUTPUT_DIR, fname)
        if os.path.exists(local_path):
            _upload(local_path, f"{PYDATA_PREFIX}/{fname}")
            uploaded.append(fname)
        else:
            log.warning("expected output not produced: %s", fname)

    result = {"job": job, "uploaded": uploaded, "elapsed_s": round(time.time() - t0, 1)}
    log.info("done: %s", result)
    return result
