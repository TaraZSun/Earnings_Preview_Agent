import argparse
import hashlib
import json
import re
from datetime import datetime, timezone

from ..core.cli import run_cli
from ..core.data_status import DataStatus, DataUnavailable
from ..core.path import get_case_dir
from .validation import validate_transcript


def clean_transcript(text):
    lines = text.splitlines()
    start = 0
    end = len(lines)
    changes = []

    # Conservative opening detection near the start of the document.
    for index, line in enumerate(lines[:30]):
        if (
            re.search(r"\bwelcome\b", line, re.I)
            and re.search(r"earnings.*(?:call|conference)", line, re.I)
        ):
            start = index

            # Preserve a short speaker-name line immediately above.
            if index > 0:
                previous = lines[index - 1].strip()
                if previous and len(previous) < 80:
                    start = index - 1

            if start > 0:
                changes.append("Removed material before call opening")
            break

    # Use an explicit closing near the end, not a generic thank-you.
    for index in range(len(lines) - 1, start - 1, -1):
        if index < len(lines) * 0.8:
            break

        if re.search(
            r"(?:this does conclude|this concludes)"
            r".{0,50}(?:conference|call)",
            lines[index],
            re.I,
        ):
            end = index + 1
            if end < len(lines):
                changes.append("Removed material after call closing")
            break

    cleaned = "\n".join(lines[start:end]).strip() + "\n"
    return cleaned, changes


def score_transcript(text, changes):
    # Formatting-based ranking after eligibility checks.
    # This score is not a factual-accuracy score.
    labels = re.findall(
        r"(?m)^[A-Z][A-Za-z .’'-]{2,70}\s*:",
        text,
    )

    unique_labels = {
        label.strip().rstrip(":").strip()
        for label in labels
    }

    components = {
        "speaker_labels": min(len(labels), 20),
        "distinct_speakers": min(len(unique_labels), 10),
    }

    return {
        "total": sum(components.values()),
        "components": components,
        "speaker_label_count": len(labels),
        "cleaning_changes": changes,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("target_quarter")
    parser.add_argument("--source-quarter", required=True)
    parser.add_argument("--company-alias", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    case_dir = get_case_dir(ticker, args.target_quarter).resolve()

    manifest_path = case_dir / "transcript_drafts" / "manifest.json"
    records = json.loads(manifest_path.read_text(encoding="utf-8"))

    eligible = []
    report = []

    for record in records:
        if record.get("status") == "failed":
            continue

        path = (case_dir / record["file"]).resolve()
        if not path.is_relative_to(case_dir):
            raise ValueError("Candidate path is outside the case directory")

        original = path.read_text(encoding="utf-8")
        cleaned, changes = clean_transcript(original)

        validation = validate_transcript(
            cleaned,
            record,
            ticker=ticker,
            source_quarter=args.source_quarter,
            company_alias=args.company_alias,
            as_of=args.as_of,
        )

        entry = {
            "file": record["file"],
            "source_url": record["source_url"],
            "validation": validation,
        }

        if validation["status"] == "passed_checks":
            score = score_transcript(cleaned, changes)
            entry["score"] = score

            eligible.append({
                "record": record,
                "text": cleaned,
                "validation": validation,
                "score": score,
                "original_sha256": hashlib.sha256(
                    original.encode("utf-8")
                ).hexdigest(),
            })

        report.append(entry)

    report_path = case_dir / "transcript_selection_report.json"
    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    if not eligible:
        raise DataUnavailable(
            DataStatus.UNVERIFIED,
            "No transcript candidate passed validation",
        )

    # Prefer a higher score; resolve ties by URL for stable results.
    eligible.sort(
        key=lambda item: (
            -item["score"]["total"],
            item["record"]["source_url"],
        )
    )
    selected = eligible[0]

    metadata = {
        "ticker": ticker,
        "target_quarter": args.target_quarter,
        "source_quarter": args.source_quarter,
        "as_of": args.as_of,
        "source_url": selected["record"]["source_url"],
        "selected_candidate": selected["record"]["file"],
        "original_sha256": selected["original_sha256"],
        "cleaned_sha256": hashlib.sha256(
            selected["text"].encode("utf-8")
        ).hexdigest(),
        "validation": selected["validation"],
        "score": selected["score"],
        "selection_method": (
            "Highest formatting score among candidates passing "
            "rule checks; ties resolved by source URL."
        ),
        "selected_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    (case_dir / "transcript.md").write_text(
        selected["text"], encoding="utf-8"
    )
    (case_dir / "transcript_metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    print(f"Selected: {metadata['selected_candidate']}")
    print(f"Source: {metadata['source_url']}")
    print(f"Score: {metadata['score']['total']}")
    print(f"Saved: {case_dir / 'transcript.md'}")


if __name__ == "__main__":
    run_cli(main)