import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

import requests
import trafilatura
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from ..core.cli import run_cli
from ..core.data_status import DataStatus, DataUnavailable
from ..core.path import ensure_case_dir
from ..core.ticker_resolver import resolve_ticker


def retrieve_consensus_candidates(
    ticker,
    company,
    quarter,
    *,
    as_of,
    report_date,
    case_dir,
    log,
):
    cutoff = date.fromisoformat(as_of)
    report = date.fromisoformat(report_date)

    if cutoff >= report:
        raise ValueError(
            "For this daily pre-earnings test, "
            "as_of must be earlier than report_date"
        )

    # Search around the event date, not only the fiscal-quarter label.
    # Search hints only; publication dates still require validation.
    window_start = cutoff - timedelta(days=30)
    before_date = cutoff + timedelta(days=1)

    date_hint = (
        f"after:{window_start.isoformat()} "
        f"before:{before_date.isoformat()}"
    )

    queries = [
        (
            f'"{ticker}" {company} earnings preview '
            f"consensus revenue EPS {date_hint}"
        ),
        (
            f'"{ticker}" earnings expected revenue '
            f"adjusted earnings {cutoff.strftime('%B %Y')} "
            f"{date_hint}"
        ),
        (
            f'site:stockstory.org "{ticker}" '
            f"earnings reports tomorrow "
            f"{cutoff.strftime('%B %Y')} {date_hint}"
        ),
    ]

    log(f"[{ticker}] Looking for historical consensus...")

    candidates = []

    for query in queries:
        log(f"[{ticker}] Searching: {query}")

        try:
            results = DDGS(timeout=20).text(
                query,
                max_results=5,
                region="us-en",
            )
            candidates.extend(results)
        except DDGSException as e:
            log(
                f"[{ticker}] Search failed: "
                f"{type(e).__name__}; trying another query."
            )

    if not candidates:
        raise DataUnavailable(
            DataStatus.FAILED,
            "Consensus search returned no usable candidates",
        )

    folder = case_dir / "consensus_drafts"
    folder.mkdir(parents=True, exist_ok=True)

    records = []
    seen = set()

    for index, candidate in enumerate(candidates, start=1):
        url = candidate.get("href", "")
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)

        record = {
            "requested_ticker": ticker,
            "requested_quarter": quarter,
            "requested_as_of": as_of,
            "requested_report_date": report_date,
            "search_title": candidate.get("title", ""),
            "search_description": candidate.get("body", ""),
            "source_url": url,
        }

        log(f"[{ticker}] Found candidate: {url}")

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
                raise ValueError("Candidate is not HTML")

            text = trafilatura.extract(
                response.content,
                url=response.url,
                include_comments=False,
                include_tables=True,
            )
            if not text or not text.strip():
                raise ValueError("No article text extracted")

            # A detected date is a hint, not verified publication evidence.
            metadata = trafilatura.extract_metadata(
                response.content,
                default_url=response.url,
            )
            detected_date = (
                getattr(metadata, "date", None)
                if metadata is not None
                else None
            )

            stem = f"candidate_{index:02d}"
            text_path = folder / f"{stem}.md"
            html_path = folder / f"{stem}.html"

            text_path.write_text(text, encoding="utf-8")
            html_path.write_bytes(response.content)

            record.update({
                "resolved_url": response.url,
                "text_file": str(text_path.relative_to(case_dir)),
                "html_file": str(html_path.relative_to(case_dir)),
                "detected_date_unverified": detected_date,
                "words": len(text.split()),
                "status": "extracted_unverified",
                "downloaded_at_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

            log(
                f"[{ticker}] Saved {stem}: "
                f"{record['words']} words; date hint={detected_date}"
            )

        except (requests.RequestException, ValueError) as e:
            record.update({
                "status": "failed",
                "error": str(e),
            })
            log(
            f"[{ticker}] Candidate failed: "
            f"{type(e).__name__}: {e}"
            )

        records.append(record)

    manifest = {
        "ticker": ticker,
        "company": company,
        "quarter": quarter,
        "as_of": as_of,
        "report_date": report_date,
        "queries": queries,
        "candidates": records,
    }

    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    if not any(
        r["status"] == "extracted_unverified"
        for r in records
    ):
        raise DataUnavailable(
            DataStatus.FAILED,
            "No consensus candidate text extracted; diagnostics are preserved",
        )
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("quarter")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--report-date", required=True)
    args = parser.parse_args()

    log = lambda message: print(
        message, file=sys.stderr, flush=True
    )

    identity = resolve_ticker(args.ticker, log=log)
    ticker = identity["ticker"]
    case_dir = ensure_case_dir(ticker, args.quarter)

    retrieve_consensus_candidates(
        ticker,
        identity["company"],
        args.quarter,
        as_of=args.as_of,
        report_date=args.report_date,
        case_dir=case_dir,
        log=log,
    )

    log(f"[{ticker}] Saved consensus candidates to {case_dir}")


if __name__ == "__main__":
    run_cli(main)