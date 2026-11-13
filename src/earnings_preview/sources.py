"""Extract data sources for the DAG pipeline: prior-quarter guidance and call transcripts.

Same contract as tools.py: return a dict, or {"error: "..."} on failure, never raise.
Mock mode follows tools.MOCK (set MOCK=1 for canned ACME data). 
"""

import html
import os
import re

from . import tools


def _fetch_transcript(ticker: str, quarter: str) -> dict:
    """Plug a transcript provider in here.
 
    Must return {"ticker", "quarter", "text", "source"}. Left unimplemented on purpose:
    scraping transcript sites has licensing and stability problems, so use a licensed API.
    """
    raise NotImplementedError


 
def get_transcript(ticker: str, quarter: str, *, case_dir=None) -> dict:
    """Earnings call transcript for `quarter` (the quarter already reported, e.g. 'Q2 2026')."""
    ticker = ticker.upper().strip()
    if tools.MOCK:
        if case_dir is None:
            return {"error": "local transcript requires case_dir"}

        path = case_dir / "transcript.md"

        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                return {"error": "local transcript is empty"}

            return {
                "ticker": ticker,
                "quarter": quarter,
                "text": text,
                "source": str(path),
            }
        except OSError as e:
            return {"error": f"cannot read local transcript: {e}"}