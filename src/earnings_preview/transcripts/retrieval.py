import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
import trafilatura
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from ..core.cli import run_cli
from ..core.data_status import DataStatus, DataUnavailable
from ..core.path import ensure_case_dir
from ..core.ticker_resolver import resolve_ticker
from .call_date import find_call_date


def search_transcript(
    ticker: str,
    source_quarter: str,
    *,
    company: str | None = None,
    log=lambda message: None,
) -> list[dict]:
    ticker = ticker.upper().strip()

    match = re.fullmatch(
        r"\s*Q([1-4])\s*(?:FY)?\s*(20\d{2})\s*",
        source_quarter,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Quarter must look like 'Q3 2024'")

    number, year = match.groups()
    identity = f"{company} ({ticker})" if company else ticker

    query = (
        f"{identity} fiscal Q{number} {year} "
        "earnings call full transcript"
    )

    log(
        f"[{ticker}] Looking for FY{year} Q{number} "
        "earnings call transcript..."
    )

    try:
        items = DDGS(timeout=20).text(
            query,
            max_results=5,
            region="us-en",
        )
    except DDGSException as e:
        raise DataUnavailable(
            status=DataStatus.FAILED,
            reason=(
                "Transcript search returned no usable results "
                "or the search service was unavailable"
            ),
        ) from e

    results = []
    seen = set()

    for item in items:
        url = item.get("href", "")
        if not url.startswith("https://") or url in seen:
            continue

        seen.add(url)
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "description": item.get("body", ""),
        })

        log(f"[{ticker}] Found candidate: {url}")

    if not results:
        raise DataUnavailable(
            status=DataStatus.MISSING,
            reason="No transcript candidates found; search may be unavailable"
        )

    return results


def extract_candidates(
    candidates: list[dict],
    *,
    ticker: str,
    source_quarter: str,
    case_dir,
    log=lambda message: None,
) -> list[dict]:
    draft_dir = case_dir / "transcript_drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)

    records = []

    for index, candidate in enumerate(candidates, start=1):
        url = candidate["url"]
        record = {
            "source_url": url,
            "search_title": candidate.get("title", ""),
            "requested_ticker": ticker,
            "requested_source_quarter": source_quarter,
        }

        log(f"[{ticker}] Downloading candidate {index}: {url}")

        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": "earnings-preview-research/0.1"
                },
                timeout=(10, 30),
            )
            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()
            if "html" not in content_type:
                raise ValueError("Candidate is not an HTML page")

            log(f"[{ticker}] Extracting candidate {index}...")

            text = trafilatura.extract(
                response.content,
                url=response.url,
                output_format="txt",
                include_comments=False,
                include_tables=False,
            )

            if not text or not text.strip():
                raise ValueError("No main text extracted")

            # These checks are screening signals, not verification.
            checks = {
                "at_least_2000_words": len(text.split()) >= 2000,
                "mentions_operator": bool(
                    re.search(r"\boperator\b", text, re.I)
                ),
                "mentions_qa": bool(
                    re.search(
                        r"question.and.answer|questions.and.answers"
                        r"|first question|next question",
                        text,
                        re.I,
                    )
                ),
            }

            filename = f"candidate_{index:02d}.md"
            path = draft_dir / filename
            path.write_text(text, encoding="utf-8")

            record.update({
                "resolved_url": response.url,
                "file": str(path.relative_to(case_dir)),
                "words": len(text.split()),
                "checks": checks,
                "status": (
                    "screening_passed_unverified"
                    if all(checks.values())
                    else "needs_review"
                ),
                "downloaded_at_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

            log(
                f"[{ticker}] Saved {filename}: "
                f"{record['words']} words; {record['status']}"
            )

        except (requests.RequestException, ValueError) as e:
            record.update({
                "status": "failed",
                "error": str(e),
            })
            log(
                f"[{ticker}] Candidate {index} failed: "
                f"{type(e).__name__}; trying next candidate."
            )

        records.append(record)

    manifest = draft_dir / "manifest.json"
    manifest.write_text(
        json.dumps(records, indent=2),
        encoding="utf-8",
    )

    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("target_quarter")
    parser.add_argument("--source-quarter", required=True)
    args = parser.parse_args()

    
    log = lambda message: print(
    message, file=sys.stderr, flush=True
    )

    try:
        identity = resolve_ticker(args.ticker, log=log)
    except (ValueError, RuntimeError) as e:
        parser.exit(1, f"Error: {e}\n")

    ticker = identity["ticker"]
    company = identity["company"]
    case_dir = ensure_case_dir(ticker, args.target_quarter)

    candidates = search_transcript(
        ticker,
        args.source_quarter,
        company=company,
        log=log,
    )

    payload = {
    "input_ticker": args.ticker,
    "ticker": ticker,
    "company": company,
    "exchange": identity["exchange"],
    "target_quarter": args.target_quarter,
    "source_quarter": args.source_quarter,
    "status": "candidates_only",
    "candidates": candidates,
    }

    path = case_dir / "transcript_candidates.json"
    path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    print(f"Saved search candidates to {path}", flush=True)
    records = extract_candidates(
    candidates,
    ticker=ticker,
    source_quarter=args.source_quarter,
    case_dir=case_dir,
    log=log,
    )
    evidence = find_call_date(
            ticker,
            args.source_quarter,
            case_dir=case_dir,
            log=log,
        )

    for record in records:
        if record["status"] != "failed":
            record.update(evidence)

    manifest_path = (
        case_dir / "transcript_drafts" / "manifest.json"
    )
    manifest_path.write_text(
        json.dumps(records, indent=2),
        encoding="utf-8",
    )

    extracted = [
        item for item in records
        if item["status"] != "failed"
    ]

    if not extracted:
        raise DataUnavailable(
            DataStatus.FAILED,
            "No candidate text extracted; drafts and diagnostics are preserved",
        )

    log(
        f"[{ticker}] Extracted {len(extracted)} candidate pages. "
        "Company, fiscal quarter, call date and completeness "
        "still require validation."
    )

if __name__ == "__main__":
    run_cli(main)