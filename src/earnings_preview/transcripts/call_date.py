import json
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
import trafilatura
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from ..core.data_status import DataStatus, DataUnavailable


def load_cached_call_date(
    ticker,
    source_quarter,
    case_dir,
    domain,
    *,
    log,
):
    if case_dir is None:
        return None

    paths = [
        case_dir / "call_date.json",
        case_dir / "transcript_metadata.json",
    ]

    for path in paths:
        if not path.is_file():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            if (
                data.get("ticker") != ticker
                or data.get("source_quarter") != source_quarter
            ):
                continue

            # Existing selection metadata stores evidence under validation.
            evidence = data.get("validation", data)

            call_date = evidence.get("call_date")
            source = evidence.get("call_date_source")
            quote = evidence.get("call_date_evidence")

            if not all(isinstance(v, str) and v.strip()
                       for v in (call_date, source, quote)):
                continue

            datetime.strptime(call_date, "%Y-%m-%d")

            parsed = urlparse(source)
            host = parsed.hostname or ""
            if parsed.scheme != "https" or not (
                host == domain or host.endswith("." + domain)
            ):
                continue

            # Require the saved quotation to identify the requested call
            # and explicitly contain the recorded date.
            number, year = source_quarter.split()
            pattern = (
                rf"\b{re.escape(number)}\s+{re.escape(year)}\b"
                rf".{{0,200}}?financial results conference call"
                rf".{{0,160}}?\bon\s+"
                rf"([A-Za-z]+\s+\d{{1,2}},\s+\d{{4}})"
            )
            match = re.search(pattern, quote, re.I | re.S)
            if not match:
                continue

            quoted_date = datetime.strptime(
                re.sub(r"\s+", " ", match.group(1)),
                "%B %d, %Y",
            ).date().isoformat()

            if quoted_date != call_date:
                continue

            log(f"[{ticker}] Reusing call-date evidence: {call_date}")

            return {
                "call_date": call_date,
                "call_date_source": source,
                "call_date_evidence": quote,
            }

        except (OSError, ValueError, TypeError, AttributeError):
            log(f"[{ticker}] Invalid date cache: {path.name}; ignoring.")

    return None

def find_call_date(ticker, source_quarter, *, case_dir=None, log=lambda message: None):
    # Start with a verified company-domain mapping.
    match = re.fullmatch(r"Q([1-4]) (20\d{2})", source_quarter)
    if not match:
        raise ValueError("Quarter must look like 'Q3 2024'")

    number, year = match.groups()
    domains = {"AAPL": "apple.com"}

    ticker = ticker.upper().strip()
    domain = domains.get(ticker)
    if not domain:
        raise DataUnavailable(
            status=DataStatus.UNSUPPORTED,
            reason=f"No official-domain adapter for {ticker}"
        )
    cached = load_cached_call_date(
        ticker,
        source_quarter,
        case_dir,
        domain,
        log=log,
    )
    if cached:
        return cached
    
    query = (
        f"site:{domain} Q{number} {year} "
        "financial results conference call"
    )

    log(f"[{ticker}] Looking for official call-date evidence...")

    try:
        candidates = DDGS(timeout=20).text(query, max_results=5)
    except DDGSException as e:
        raise DataUnavailable(
            status=DataStatus.FAILED,
            reason="Official call-date search returned no usable results "
                    "or the search service was unavailable"
        ) from e

    date_pattern = (
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4}"
    )

    evidence_by_date = {}

    for candidate in candidates:
        url = candidate.get("href", "")
        host = urlparse(url).hostname or ""

        if not (
            url.startswith("https://")
            and (host == domain or host.endswith("." + domain))
        ):
            continue

        try:
            response = requests.get(url, timeout=(10, 30))
            response.raise_for_status()

            # Verify redirects still lead to the official domain.
            final_host = urlparse(response.url).hostname or ""
            if not (
                final_host == domain
                or final_host.endswith("." + domain)
            ):
                continue

            text = trafilatura.extract(
                response.content,
                url=response.url,
                include_comments=False,
            ) or ""

            # Apple-specific wording: match the target quarter and
            # the date explicitly attached to its conference call.
            pattern = (
                rf"[^.\n]*\bQ{number}\s+{year}\b"
                rf"[^.\n]*financial results conference call"
                rf".{{0,160}}?\bon\s+({date_pattern})"
            )

            for found in re.finditer(pattern, text, re.I):
                raw_date = re.sub(r"\s+", " ", found.group(1))
                call_date = datetime.strptime(
                    raw_date, "%B %d, %Y"
                ).date().isoformat()

                evidence_by_date.setdefault(call_date, {
                    "call_date": call_date,
                    "call_date_source": response.url,
                    "call_date_evidence": found.group(0).strip(),
                })

        except requests.RequestException:
            log(f"[{ticker}] Could not fetch date source; trying next.")

    if not evidence_by_date:
        raise DataUnavailable(
            status=DataStatus.UNVERIFIED,
            reason="No explicit official call-date evidence found"
        )

    if len(evidence_by_date) != 1:
        raise DataUnavailable(
            status=DataStatus.UNVERIFIED,
            reason="Conflicting official call dates found"
        )

    evidence = next(iter(evidence_by_date.values()))
    log(f"[{ticker}] Found call date: {evidence['call_date']}")
    
    if case_dir is not None:
        payload = {
            "ticker": ticker,
            "source_quarter": source_quarter,
            **evidence,
        }
        (case_dir / "call_date.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    
    return evidence