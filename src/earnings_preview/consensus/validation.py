import argparse
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from ..core.cli import run_cli
from ..core.data_status import DataStatus, DataUnavailable
from ..core.path import get_case_dir


def read_case_file(case_dir, relative_path):
    path = (case_dir / relative_path).resolve()
    if not path.is_relative_to(case_dir.resolve()):
        raise ValueError("Candidate path is outside case directory")
    return path.read_text(encoding="utf-8")


def unique_metadata_date(html, field):
    values = re.findall(
        rf'"{re.escape(field)}"\s*:\s*"([^"]+)"',
        html,
    )

    if not values:
        raise ValueError(f"{field} not found in HTML metadata")

    dates = {date.fromisoformat(value[:10]) for value in values}
    if len(dates) != 1:
        raise ValueError(f"Ambiguous {field} metadata")

    return next(iter(dates))


def validate_stockstory(text, html, record, *, ticker, cutoff, report):
    url = record.get("resolved_url") or record["source_url"]
    host = urlparse(url).hostname or ""

    if host not in {"stockstory.org", "www.stockstory.org"}:
        raise ValueError("Source adapter not implemented")

    if "/news/earnings/" not in urlparse(url).path:
        raise ValueError("Not a supported earnings article")

    published = unique_metadata_date(html, "datePublished")
    modified = unique_metadata_date(html, "dateModified")

    if published > cutoff:
        raise ValueError("Article published after cutoff")

    if modified > cutoff:
        raise ValueError(
            "Article modified after cutoff; historical text not established"
        )

    if not re.search(
        rf"\b{re.escape(ticker)}\b",
        text[:2000],
        re.I,
    ):
        raise ValueError("Ticker not found in article opening")

    # Require the visible article date to agree with HTML metadata.
    visible_date = re.search(
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4}\b",
        text[:1500],
    )
    if not visible_date:
        raise ValueError("Visible publication date missing")

    visible = datetime.strptime(
        visible_date.group(0), "%B %d, %Y"
    ).date()
    if visible != published:
        raise ValueError("Visible date and publication metadata disagree")

    # This adapter supports articles explicitly announcing results tomorrow.
    event = re.search(
        r"[^.\n]*will be announcing earnings results "
        r"tomorrow after the bell[^.\n]*[.!]?",
        text[:3000],
        re.I,
    )
    if not event or published + timedelta(days=1) != report:
        raise ValueError("Expected earnings event not established")

    revenue = re.search(
        r"This quarter,\s+analysts are expecting[^.\n]*?"
        r"revenue to grow\s+([\d.]+)%[^.\n]*?"
        r"to\s+\$([\d,]+(?:\.\d+)?)\s+(billion|million)",
        text,
        re.I,
    )
    eps = re.search(
        r"Adjusted earnings are expected to come in at\s+"
        r"\$([\d,]+(?:\.\d+)?)\s+per share",
        text,
        re.I,
    )

    if not revenue or not eps:
        raise ValueError("Supported consensus wording not found")

    amount = Decimal(revenue.group(2).replace(",", ""))
    revenue_m = (
        amount * 1000
        if revenue.group(3).lower() == "billion"
        else amount
    )
    eps_value = Decimal(eps.group(1).replace(",", ""))

    if revenue_m <= 0 or eps_value <= 0:
        raise ValueError("Invalid values for this source adapter")

    return {
        "eps_estimate": float(eps_value),
        "eps_basis": "adjusted EPS",
        "revenue_estimate_usd_m": float(revenue_m),
        "revenue_estimate_usd_b_display": f"{revenue_m / 1000:.2f}",
        "revenue_growth_estimate_pct": float(revenue.group(1)),
        "source": "StockStory earnings preview",
        "source_url": url,
        "source_published_date": published.isoformat(),
        "source_modified_date": modified.isoformat(),
        "evidence": {
            "publication_date": visible_date.group(0),
            "reporting_event": event.group(0).strip(),
            "revenue": revenue.group(0),
            "eps": eps.group(0),
        },
        "candidate_text_file": record["text_file"],
        "candidate_html_file": record["html_file"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("quarter")
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    case_dir = get_case_dir(ticker, args.quarter).resolve()
    folder = case_dir / "consensus_drafts"

    manifest = json.loads(
        (folder / "manifest.json").read_text(encoding="utf-8")
    )

    if (
        manifest["ticker"] != ticker
        or manifest["quarter"] != args.quarter
    ):
        raise ValueError("Manifest does not match requested case")

    cutoff = date.fromisoformat(manifest["as_of"])
    report = date.fromisoformat(manifest["report_date"])

    accepted = []
    checks = []

    for record in manifest["candidates"]:
        if record.get("status") == "failed":
            continue

        try:
            text = read_case_file(case_dir, record["text_file"])
            html = read_case_file(case_dir, record["html_file"])

            proposal = validate_stockstory(
                text,
                html,
                record,
                ticker=ticker,
                cutoff=cutoff,
                report=report,
            )
        except (OSError, ValueError, KeyError) as e:
            checks.append({
                "source_url": record.get("source_url"),
                "status": "not_accepted",
                "reason": str(e),
            })
            continue

        accepted.append(proposal)
        checks.append({
            "source_url": proposal["source_url"],
            "status": "passed_adapter_checks",
        })

    (folder / "validation_report.json").write_text(
        json.dumps(checks, indent=2),
        encoding="utf-8",
    )

    if not accepted:
        raise DataUnavailable(
            DataStatus.UNVERIFIED,
            "No consensus candidate passed the supported source checks",
        )

    # Prefer newer qualifying articles; use URL as a stable tiebreaker.
    accepted.sort(
        key=lambda item: (
            -date.fromisoformat(
                item["source_published_date"]
            ).toordinal(),
            item["source_url"],
        )
    )

    proposed = {
        "ticker": ticker,
        "company": manifest["company"],
        "quarter": args.quarter,
        "as_of": cutoff.isoformat(),
        "next_earnings_date": report.isoformat(),
        **accepted[0],
        "history": [],
        "status": "proposed",
        "limitations": [
            "Fiscal quarter assignment uses the requested event mapping",
            "Detailed EPS adjustment methodology not specified",
            "Historical EPS surprise data not supplied",
            "Downloaded article is not an archived historical snapshot"
        ],
    }

    path = case_dir / "consensus_proposed.json"
    path.write_text(
        json.dumps(proposed, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(proposed, indent=2))
    print(f"\nSaved proposal to {path}")


if __name__ == "__main__":
    run_cli(main)