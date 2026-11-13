import argparse
import json
import re
from datetime import date
from pathlib import Path

from ..core.path import get_case_dir


def validate_transcript(
    text: str,
    record: dict,
    *,
    ticker: str,
    source_quarter: str,
    company_alias: str,
    as_of: str,
) -> dict:
    cutoff = date.fromisoformat(as_of)

    match = re.fullmatch(r"Q([1-4]) (20\d{2})", source_quarter)
    if not match:
        raise ValueError("source_quarter must look like 'Q3 2024'")

    number, year = match.groups()
    rejected = []
    missing = []
    checks = {}

    # Inspect the opening, not arbitrary mentions elsewhere in the call.
    opening = text[:4000]

    checks["company_in_opening"] = bool(
        re.search(
            rf"\b{re.escape(company_alias)}\b",
            opening,
            re.IGNORECASE,
        )
    )
    if not checks["company_in_opening"]:
        missing.append("Company identity not established in opening")

    # Supports examples such as Q3 2024, Q3 FY2024,
    # Q3 Fiscal Year 2024 and FY2024 Q3.
    period_patterns = [
        r"\bQ([1-4])\s+(?:FY\s*|Fiscal\s+Year\s+)?(20\d{2})\b",
        r"\bFY\s*(20\d{2})\s+Q([1-4])\b",
    ]

    observed_periods = set()

    for item in re.finditer(period_patterns[0], opening, re.I):
        observed_periods.add(item.groups())

    for item in re.finditer(period_patterns[1], opening, re.I):
        observed_year, observed_number = item.groups()
        observed_periods.add((observed_number, observed_year))

    expected_period = (number, year)
    checks["source_quarter_in_opening"] = (
        expected_period in observed_periods
    )

    if observed_periods and expected_period not in observed_periods:
        rejected.append("Opening identifies a different fiscal quarter")
    elif observed_periods != {expected_period}:
        missing.append("Fiscal quarter is missing or ambiguous")

    checks["substantial_body"] = len(text.split()) >= 2000
    checks["has_qa_signal"] = bool(
        re.search(
            r"question.and.answer|questions.and.answers"
            r"|first question|next question",
            text,
            re.I,
        )
    )
    checks["has_closing_signal"] = bool(
        re.search(
            r"concludes?.{0,30}(?:call|conference)"
            r"|thank you.{0,30}joining us",
            text[-5000:],
            re.I,
        )
    )

    for name in (
        "substantial_body",
        "has_qa_signal",
        "has_closing_signal",
    ):
        if not checks[name]:
            missing.append(f"Completeness signal missing: {name}")

    # Never use downloaded_at_utc or a dividend date as the call date.
    call_date_value = record.get("call_date")
    date_source = record.get("call_date_source")
    date_evidence = record.get("call_date_evidence")

    checks["call_date_documented"] = bool(
        call_date_value and date_source and date_evidence
    )

    if not checks["call_date_documented"]:
        missing.append("Call date and its source evidence are missing")
    else:
        try:
            call_date = date.fromisoformat(call_date_value)
        except ValueError:
            missing.append("Call date is not a valid ISO date")
        else:
            if call_date > cutoff:
                rejected.append("Call date is later than the cutoff")
            # Broad sanity check, not a fiscal-calendar conversion.
            if abs(call_date.year - int(year)) > 1:
                rejected.append("Call date conflicts with fiscal year")

    status = (
        "rejected"
        if rejected
        else "needs_review"
        if missing
        else "passed_checks"
    )

    return {
        "ticker": ticker,
        "source_quarter": source_quarter,
        "as_of": cutoff.isoformat(),
        "source_url": record.get("source_url"),
        "file": record.get("file"),
        "status": status,
        "checks": checks,
        "rejection_reasons": rejected,
        "review_reasons": missing,
        "call_date": call_date_value,
        "call_date_source": date_source,
        "call_date_evidence": date_evidence,
        "limitation": (
            "Rule checks do not independently prove transcript "
            "accuracy or completeness."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("target_quarter")
    parser.add_argument("--source-quarter", required=True)
    parser.add_argument("--company-alias", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()

    case_dir = get_case_dir(args.ticker, args.target_quarter)
    manifest_path = case_dir / "transcript_drafts" / "manifest.json"
    records = json.loads(manifest_path.read_text(encoding="utf-8"))

    results = []

    for record in records:
        if record.get("status") == "failed":
            continue

        path = (case_dir / record["file"]).resolve()

        # Only read files belonging to this case.
        if not path.is_relative_to(case_dir.resolve()):
            raise ValueError("Candidate path is outside the case directory")

        text = path.read_text(encoding="utf-8")

        results.append(validate_transcript(
            text,
            record,
            ticker=args.ticker.upper().strip(),
            source_quarter=args.source_quarter,
            company_alias=args.company_alias,
            as_of=args.as_of,
        ))

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()