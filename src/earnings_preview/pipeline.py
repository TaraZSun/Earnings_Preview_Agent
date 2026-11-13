"""Earnings preview pipeline: a fixed DAG with a single LLM call.

    python -m earnings_preview.pipeline AAPL "Q4 2026"

Steps (same order every run):
    1. consensus + price action     (yfinance)
    2. prior guidance               (last earnings press release on EDGAR)
    3. qualitative context          (last quarter's call transcript; falls back to the
                                     latest 10-Q/10-K MD&A when no transcript is available)
    4. one LLM call -> preview, max 300 words (one rewrite attempt if over)
    5. number check: flag any figure in the note that is not in the gathered data

A failed source never stops the run; it becomes a line under "Data gaps" in the note.
The run only aborts if consensus, guidance and qualitative context are all missing.
"""


import argparse
import json
import re
import sys

from .checks.numbers import check_numbers
from .core.config import MODEL
from .core.path import PROMPT_DIR, ensure_case_dir
from .workflow.run_manifest import load_manifest, read_authorized_file
from .sources import get_transcript
from .tools import (
    get_earnings_context,
    get_price_action,
    list_recent_filings,
    read_filing_section,
)

WORD_LIMIT = 300
TARGET_WORDS = 260  # ask for less than the limit so small overshoots still pass
PROMPT_PATH = PROMPT_DIR / "earnings_preview.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text(
    encoding="utf-8"
).replace("{TARGET_WORDS}", str(TARGET_WORDS))


# ---------------------------------------------------------------- quarters
def parse_quarter(q: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*Q([1-4])\s*(?:FY)?\s*(20\d\d)\s*", q, re.I)
    if not m:
        raise ValueError(f"quarter must look like 'Q4 2026', got {q!r}")
    return int(m.group(1)), int(m.group(2))


def norm_quarter(q: str) -> str:
    n, y = parse_quarter(q)
    return f"Q{n} {y}"


def prior_quarter(q: str) -> str:
    n, y = parse_quarter(q)
    return f"Q{n - 1} {y}" if n > 1 else f"Q4 {y - 1}"

def calculate_revenue_comparison(
    ticker: str,
    quarter: str,
    consensus: dict | None,
    *,
    case_dir,
    guidance_data=None,
) -> dict:
    path = case_dir / "guidance.json"
    if guidance_data is None and not path.is_file():
        return {
            "error": (
                "No supported structured revenue guidance supplied; "
                "use the transcript for qualitative guidance analysis."
            )
        }
    try:
        guidance = (
            guidance_data
            if guidance_data is not None
            else json.loads(path.read_text(encoding="utf-8"))
        )
        if (
            guidance["ticker"] != ticker
            or guidance["quarter"] != quarter
        ):
            raise ValueError("guidance ticker or quarter mismatch")

        if (
            guidance["currency"] != "USD"
            or guidance["unit"] != "billion"
            or guidance["basis"] != "reported revenue"
        ):
            raise ValueError("unexpected guidance units or basis")

        expected_segments = {
            "Productivity and Business Processes",
            "Intelligent Cloud",
            "More Personal Computing",
        }
        segments = guidance["segments"]

        if (
            len(segments) != 3
            or {s["name"] for s in segments} != expected_segments
        ):
            raise ValueError("expected all three Microsoft segments")

        for segment in segments:
            if not 0 < segment["low"] <= segment["high"]:
                raise ValueError("invalid segment guidance range")

        low = round(sum(s["low"] for s in segments), 2)
        high = round(sum(s["high"] for s in segments), 2)
        midpoint = round((low + high) / 2, 2)

        if not consensus:
            raise ValueError("consensus unavailable")

        if (
            consensus["ticker"] != ticker
            or consensus["quarter"] != quarter
        ):
            raise ValueError("consensus ticker or quarter mismatch")

        revenue_m = consensus.get("revenue_estimate_usd_m")
        if revenue_m is None or revenue_m <= 0:
            raise ValueError("revenue consensus missing or invalid")

        
        revenue_b = revenue_m / 1000

        if revenue_b < low:
            position = "below"
        elif revenue_b > high:
            position = "above"
        else:
            position = "inside"

        return {
            "ticker": ticker,
            "quarter": quarter,
            "currency": "USD",
            "unit": "billion",
            "method": "sum of the three segment revenue guidance ranges",
            "derived_guidance_low": low,
            "derived_guidance_high": high,
            "derived_guidance_midpoint": midpoint,
            "consensus_revenue": round(revenue_b, 5),
            "consensus_revenue_display": f"{revenue_b:.2f}",
            "consensus_position": position,
            "consensus_vs_midpoint_pct": round(
                (revenue_b / midpoint - 1) * 100,
                2,
            ),
            "source_url": guidance["source_url"],
            "caveat": (
                "Calculated from segment guidance; "
                "not an explicitly stated consolidated guidance range."
            ),
        }

    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"error": f"revenue comparison unavailable: {e}"}

def gather_from_manifest(
    ticker,
    quarter,
    manifest_path,
    log=lambda message: None,
):
    manifest = load_manifest(
        manifest_path,
        ticker=ticker,
        quarter=quarter,
    )

    gaps = []

    def read_dataset(name, *, json_data=True):
        entry = manifest["datasets"][name]
        content = read_authorized_file(manifest, name)

        if content is None:
            gaps.append(
                f"{name}: {entry['status']} — {entry['reason']}"
            )
            log(f"{name} -> {entry['status']}")
            return None

        log(f"{name} -> {entry['status']}")

        if entry["status"] == "partial":
            gaps.append(
                f"{name}: partial — "
                f"{entry['reason'] or 'Incomplete coverage'}"
            )

        if not json_data:
            text = content.decode("utf-8")
            if not text.strip():
                raise ValueError(f"Authorized {name} text is empty")
            return text

        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError(f"Authorized {name} must be a JSON object")

        if data.get("ticker") != ticker:
            raise ValueError(f"Authorized {name} ticker mismatch")

        return data

    consensus = read_dataset("consensus")
    price = read_dataset("prices")
    transcript = read_dataset("transcript", json_data=False)
    guidance = read_dataset("guidance")

    if consensus is not None:
        if (
            consensus.get("quarter") != quarter
            or consensus.get("as_of") != manifest["as_of"]
            or consensus.get("next_earnings_date")
            != manifest["report_date"]
        ):
            raise ValueError("Authorized consensus context mismatch")

    if price is not None:
        requested = price.get(
            "requested_as_of",
            price.get("as_of"),
        )
        if (
            requested != manifest["as_of"]
            or not price.get("as_of")
            or price["as_of"] > manifest["as_of"]
        ):
            raise ValueError("Authorized price cutoff mismatch")

    comparison = None
    if guidance is not None:
        comparison = calculate_revenue_comparison(
            ticker,
            quarter,
            consensus,
            case_dir=ensure_case_dir(ticker, quarter),
            guidance_data=guidance,
        )
        if "error" in comparison:
            gaps.append(f"revenue comparison: {comparison['error']}")
            comparison = None

    qualitative = None
    if transcript is not None:
        qualitative = {
            "kind": f"{prior_quarter(quarter)} earnings call transcript",
            "text": transcript,
            "source": manifest["datasets"]["transcript"]["file"],
        }

    return {
        "ticker": ticker,
        "quarter": quarter,
        "as_of": manifest["as_of"],
        "report_date": manifest["report_date"],
        "run_id": manifest["run_id"],
        "consensus": consensus,
        "price_action": price,
        "revenue_comparison": comparison,
        "qualitative": qualitative,
        "gaps": gaps,
    }

# ------------------------------------------------------------------ gather
def gather(ticker: str, 
           quarter: str, 
           log=lambda m: None,
           *,
           manifest_path=None,
           ) -> dict:
    ticker = ticker.upper().strip()
    quarter = norm_quarter(quarter)
    if manifest_path is not None:
        return gather_from_manifest(
            ticker,
            quarter,
            manifest_path,
            log,
        )

    case_dir = ensure_case_dir(ticker, quarter)
    prior = prior_quarter(quarter)
    gaps: list[str] = []

    def step(label: str, result: dict):
        if "error" in result:
            gaps.append(f"{label}: {result['error']}")
            log(f"{label} -> ERROR")
            return None
        log(f"{label} -> ok")
        return result

    consensus = step("consensus", get_earnings_context(ticker, quarter=quarter, case_dir=case_dir))
    price = step("price action", get_price_action(ticker, case_dir=case_dir))
    revenue_comparison = step(
    "revenue comparison",
    calculate_revenue_comparison(ticker, quarter, consensus, case_dir=case_dir),
    )
    qualitative = None
    t = get_transcript(ticker, prior, case_dir=case_dir)
    if "error" not in t:
        log(f"transcript {prior} -> ok")
        qualitative = {"kind": f"{prior} earnings call transcript", "text": t["text"], "source": t.get("source")}
    else:
        log(f"transcript {prior} -> unavailable, trying filing MD&A")
        filings = list_recent_filings(ticker, forms=["10-Q", "10-K"], limit=1)
        mdna = None
        if "error" not in filings and filings["filings"]:
            mdna = read_filing_section(filings["filings"][0]["url"], "mdna", 4000)
        if mdna and "error" not in mdna:
            qualitative = {"kind": "latest 10-Q/10-K MD&A (not a call transcript)", "text": mdna["text"], "source": mdna.get("source")}
            gaps.append(f"transcript for {prior} unavailable ({t['error']}); used latest filing MD&A instead")
        else:
            gaps.append(f"transcript for {prior} and filing MD&A both unavailable ({t['error']})")

    return {
        "ticker": ticker,
        "quarter": quarter,
        "consensus": consensus,
        "price_action": price,
        "revenue_comparison": revenue_comparison,
        "qualitative": qualitative,
        "gaps": gaps,
    }


# ---------------------------------------------------------------- generate
def build_prompt(data: dict) -> str:
    return (
        f"Write the {data['ticker']} {data['quarter']} earnings preview.\n"
        f"Gaps to list under Data gaps, verbatim: {json.dumps(data['gaps'])}\n\n"
        f"DATA (JSON):\n{json.dumps(data, default=str)}"
    )


def word_count(note: str) -> int:
    return len(re.findall(r"\S+", note))


def _call(client, messages: list) -> str:
    resp = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=messages,
        max_output_tokens=4000,
    )

    if resp.status != "completed":
        raise RuntimeError(
            f"response status: {resp.status}; "
            f"details: {resp.incomplete_details}"
        )

    note = resp.output_text or ""
    if not note.strip():
        raise RuntimeError("model returned no preview")

    return note

def run_pipeline(ticker: str, 
                quarter: str, 
                client=None, 
                verbose: bool = True,
                *,
                manifest_path=None) -> dict:
    log = (lambda m: print(f"  {m}", file=sys.stderr)) if verbose else (lambda m: None)
    data = gather(ticker, quarter, log, manifest_path=manifest_path)

    if not (data["consensus"] or data["qualitative"]):
        return {"note": "", "error": "no usable data from any source", "gaps": data["gaps"], "data": data}

    if client is None:
        from openai import OpenAI
        client = OpenAI()

    messages = [{"role": "user", "content": build_prompt(data)}]
    note = _call(client, messages)
    n = word_count(note)
    if n > WORD_LIMIT:
        log(f"note is {n} words, asking for a shorter rewrite")
        messages += [
            {"role": "assistant", "content": note},
            {"role": "user", "content": f"That is {n} words. Rewrite in under {TARGET_WORDS} words, same facts, same format."},
        ]
        note = _call(client, messages)
        n = word_count(note)

    return {
        "note": note,
        "words": n,
        "over_limit": n > WORD_LIMIT,
        "unverified_numbers": check_numbers(note, [json.dumps(data, default=str)]),
        "gaps": data["gaps"],
        "data": data,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("quarter")
    parser.add_argument("--manifest")
    args = parser.parse_args()

    try:
        result = run_pipeline(
            args.ticker,
            args.quarter,
            manifest_path=args.manifest,
        )
    except ValueError as e:
        sys.exit(str(e))
    if result.get("error"):
        sys.exit(f"[!] {result['error']}: {'; '.join(result['gaps'])}")
    print(result["note"])
    print(f"\n({result['words']} words)", file=sys.stderr)
    if result["over_limit"]:
        print(f"[!] Still over {WORD_LIMIT} words after one rewrite", file=sys.stderr)
    if result["unverified_numbers"]:
        print(f"[!] Numbers not found in the gathered data, please verify: {', '.join(result['unverified_numbers'])}", file=sys.stderr)