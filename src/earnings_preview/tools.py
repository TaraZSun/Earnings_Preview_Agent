"""Data tools for the earnings-preview agent.

Every tool returns a JSON-serialisable dict. On failure it returns {"error": "..."}
instead of raising, so the agent can see the failure and decide what to do
(retry, try another source, or flag a data gap in the note).

Set MOCK=1 to read local MSFT FY2024 Q4 test data.
SEC filings are not supplied in local test mode.
"""
import html
import json
import os
import re
from pathlib import Path

import yfinance as yf

MOCK = os.getenv("MOCK") == "1"
SEC_UA = os.getenv("SEC_USER_AGENT", "earnings-preview-agent sunzhiying321@gmail.com")


# ------------------------------------------------------------------- tools
def get_earnings_context(ticker: str, 
                        *,
                        quarter: str | None = None,
                        case_dir=None,
                        ) -> dict:
    """Next earnings date, consensus estimates, and the last four quarters of EPS surprises."""
    ticker = ticker.upper().strip()
    if MOCK:
        if case_dir is None or quarter is None:
            return {"error": "local consensus requires case_dir and quarter"}
        path = case_dir / "consensus.json"
    
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["ticker"] != ticker or data["quarter"] != quarter:
                return {"error": "local consensus does not match request"}
            return data
        except (OSError, ValueError, KeyError) as e:
            return {"error": f"cannot read local consensus: {e}"}

    try:
        t = yf.Ticker(ticker)
        out = {"ticker": ticker, "company": t.info.get("longName"), "source": "yfinance"}
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        out["next_earnings_date"] = str(dates[0]) if dates else None
        out["eps_estimate"] = cal.get("Earnings Average")
        rev = cal.get("Revenue Average")
        out["revenue_estimate_usd_m"] = round(rev / 1e6, 1) if rev else None
        hist = t.earnings_history
        out["history"] = []
        if hist is not None and len(hist):
            for idx, row in hist.tail(4).iterrows():
                est, act = row.get("epsEstimate"), row.get("epsActual")
                out["history"].append({
                    "quarter": str(idx)[:10],
                    "eps_estimate": est,
                    "eps_actual": act,
                    "surprise_pct": round((act - est) / abs(est) * 100, 1) if est else None,
                })
        return out
    except Exception as e:  # network, schema drift, bad ticker...
        return {"error": f"get_earnings_context failed: {type(e).__name__}: {e}"}


def get_price_action(ticker: str, benchmark: str = "SPY", *, case_dir=None) -> dict:
    """Recent returns vs a benchmark, 52-week range, average volume."""
    ticker = ticker.upper().strip()
    benchmark = benchmark.upper().strip()
    if MOCK:
        if case_dir is None:
            return {"error": "local price data requires case_dir"}

        path = case_dir / "price_action.json"

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["ticker"] != ticker or data["benchmark"] != benchmark:
                return {"error": "local price data does not match request"}
            return data
        except (OSError, ValueError, KeyError) as e:
            return {"error": f"cannot read local price data: {e}"}
    try:
        px = yf.Ticker(ticker).history(period="1y")["Close"]
        bm = yf.Ticker(benchmark).history(period="1y")["Close"]
        if len(px) < 70:
            return {"error": f"not enough price history for {ticker}"}

        def ret(s, n):
            return round((s.iloc[-1] / s.iloc[-n] - 1) * 100, 1)

        vol = yf.Ticker(ticker).history(period="3mo")["Volume"].mean()
        return {
            "ticker": ticker,
            "last_close": round(float(px.iloc[-1]), 2),
            "return_1m_pct": ret(px, 21),
            "return_3m_pct": ret(px, 63),
            "benchmark": benchmark,
            "benchmark_return_3m_pct": ret(bm, 63),
            "high_52w": round(float(px.max()), 2),
            "low_52w": round(float(px.min()), 2),
            "avg_daily_volume_3m": int(vol),
            "source": "yfinance",
        }
    except Exception as e:
        return {"error": f"get_price_action failed: {type(e).__name__}: {e}"}


def _sec_get(url: str):
    import requests

    r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=20)
    r.raise_for_status()
    return r


def list_recent_filings(ticker: str, forms: list[str] | None = None, limit: int = 5) -> dict:
    """Most recent SEC filings (default 10-K, 10-Q, 8-K) with URLs."""
    ticker = ticker.upper().strip()
    forms = forms or ["10-K", "10-Q", "8-K"]
    if MOCK:
        return {
            "error": f"local SEC filings not supplied for {ticker}"
        }
    try:
        tickers = _sec_get("https://www.sec.gov/files/company_tickers.json").json()
        cik = next((v["cik_str"] for v in tickers.values() if v["ticker"] == ticker), None)
        if cik is None:
            return {"error": f"ticker {ticker} not found in SEC company list"}
        sub = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
        out = []
        for form, filed, acc, doc, desc in zip(
            sub["form"], sub["filingDate"], sub["accessionNumber"], sub["primaryDocument"], sub["primaryDocDescription"]
        ):
            if form in forms:
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                out.append({"form": form, "filed": filed, "url": url, "description": desc})
            if len(out) >= limit:
                break
        return {"ticker": ticker, "filings": out, "source": "sec-edgar"}
    except Exception as e:
        return {"error": f"list_recent_filings failed: {type(e).__name__}: {e}"}


_SECTION_PATTERNS = {
    "mdna": r"management.s\s+discussion\s+and\s+analysis",
    "risk": r"risk\s+factors",
}


def read_filing_section(url: str, section: str, max_chars: int = 6000) -> dict:
    """Extract the MD&A ('mdna') or Risk Factors ('risk') text from a filing URL, truncated."""
    if section not in _SECTION_PATTERNS:
        return {"error": f"section must be one of {list(_SECTION_PATTERNS)}"}
    if MOCK:
        return {
            "error": "local filing sections not supplied"
        }
    try:
        raw = _sec_get(url).text
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text)
        hits = [m.start() for m in re.finditer(_SECTION_PATTERNS[section], text, flags=re.I)]
        if not hits:
            return {"error": f"section '{section}' not found in filing"}
        start = hits[1] if len(hits) > 1 else hits[0]  # first hit is usually the table of contents
        return {"url": url, "section": section, "text": text[start:start + max_chars], "source": "sec-edgar"}
    except Exception as e:
        return {"error": f"read_filing_section failed: {type(e).__name__}: {e}"}


# ------------------------------------------------------ registry for the LLM
TOOL_FUNCS = {
    "get_earnings_context": get_earnings_context,
    "get_price_action": get_price_action,
    "list_recent_filings": list_recent_filings,
    "read_filing_section": read_filing_section,
}

TOOL_SCHEMAS = [
    {
        "name": "get_earnings_context",
        "description": "Next earnings date, consensus EPS/revenue, and last four quarters of EPS surprises for a US-listed ticker.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_price_action",
        "description": "Recent price performance vs a benchmark (1m/3m returns, 52-week range, avg volume).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "benchmark": {"type": "string", "default": "SPY"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "list_recent_filings",
        "description": "List the most recent SEC filings (10-K, 10-Q, 8-K) for a ticker, with URLs usable by read_filing_section.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "forms": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "read_filing_section",
        "description": "Read the MD&A ('mdna') or Risk Factors ('risk') section of a filing, by URL from list_recent_filings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "section": {"type": "string", "enum": ["mdna", "risk"]},
                "max_chars": {"type": "integer", "default": 6000},
            },
            "required": ["url", "section"],
        },
    },
]