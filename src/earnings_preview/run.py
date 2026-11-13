import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .core.cli import DATA_UNAVAILABLE_EXIT_CODE
from .core.data_status import DataStatus, DataUnavailable
from .core.path import OUTPUT_DIR, PROJECT_ROOT, ensure_case_dir
from .workflow.run_manifest import (
    authorize_file,
    create_manifest,
    mark_unavailable,
    save_manifest,
)
from .core.ticker_resolver import resolve_ticker


def log(message):
    print(message, file=sys.stderr, flush=True)


def normalize_quarter(value):
    match = re.fullmatch(
        r"\s*Q([1-4])\s*(?:FY)?\s*(20\d{2})\s*",
        value,
        re.I,
    )
    if not match:
        raise ValueError("Quarter must look like 'Q4 2024'")

    number, year = map(int, match.groups())
    quarter = f"Q{number} {year}"
    prior = (
        f"Q{number - 1} {year}"
        if number > 1
        else f"Q4 {year - 1}"
    )
    return quarter, prior


def run_step(module, arguments, *, env, capture=False):
    command = [
        sys.executable,
        "-m",
        f"{__package__}.{module}",
        *arguments,
    ]

    log(f"\nRunning {module}...")
    started_ns = time.time_ns()

    # Capture logs so the structured failure message can be inspected.
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)

    if not capture and result.stdout:
        print(result.stdout, end="", flush=True)

    if result.returncode == DATA_UNAVAILABLE_EXIT_CODE:
        messages = [
            line.removeprefix("DATA_UNAVAILABLE:")
            for line in result.stderr.splitlines()
            if line.startswith("DATA_UNAVAILABLE:")
        ]

        if len(messages) != 1:
            raise ValueError(
                f"{module}: invalid data-unavailability protocol"
            )

        payload = json.loads(messages[0])

        if payload.get("file") is not None:
            raise ValueError(
                "Unavailable data must not authorize a file"
            )

        raise DataUnavailable(
            DataStatus(payload["status"]),
            payload["reason"],
        )

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
        )

    return result.stdout, started_ns


def fresh_bytes(path, started_ns):
    if not path.is_file():
        raise ValueError(
            f"Step succeeded but expected output is missing: {path}"
        )

    if path.stat().st_mtime_ns < started_ns:
        raise ValueError(
            f"Step did not produce a fresh output: {path}"
        )

    content = path.read_bytes()
    if not content.strip():
        raise ValueError(f"Step produced an empty file: {path}")

    return content


def fresh_json(path, started_ns):
    content = fresh_bytes(path, started_ns)
    data = json.loads(content)

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")

    return data, content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("quarter")
    parser.add_argument("--as-of")
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--company-alias")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    manifest = None
    manifest_path = None

    try:
        quarter, prior = normalize_quarter(args.quarter)
        cutoff = (
            date.fromisoformat(args.as_of)
            if args.as_of
            else datetime.now(ZoneInfo("Asia/Shanghai")).date()
        )
        report = date.fromisoformat(args.report_date)

        if cutoff >= report:
            raise ValueError(
                "as_of must be earlier than report_date "
                "for this daily pre-earnings workflow"
            )

        identity = resolve_ticker(args.ticker, log=log)
        ticker = identity["ticker"]

        alias = args.company_alias or re.sub(
            r"\s+(?:Inc\.?|Corporation|Corp\.?|Ltd\.?|plc)$",
            "",
            identity["company"],
            flags=re.I,
        ).strip(" ,")

        cutoff_string = cutoff.isoformat()
        report_string = report.isoformat()
        case_dir = ensure_case_dir(ticker, quarter).resolve()
        env = os.environ.copy()

        manifest, manifest_path = create_manifest(
            ticker,
            quarter,
            cutoff_string,
            report_string,
        )
        manifest["company"] = identity["company"]
        manifest["workflow_status"] = "preparing"
        manifest["provenance"] = {}
        save_manifest(manifest, manifest_path)

        log(f"Run manifest: {manifest_path}")

        def collect(dataset, producer):
            try:
                content, filename, provenance = producer()
            except DataUnavailable as error:
                mark_unavailable(
                    manifest,
                    dataset,
                    error.status,
                    error.reason,
                )
                save_manifest(manifest, manifest_path)
                log(
                    f"[{ticker}] {dataset}: "
                    f"{error.status.value} — {error.reason}"
                )
                return

            # Snapshot this run's successful output before authorizing it.
            snapshot = manifest_path.parent / "inputs" / filename
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(content)

            authorize_file(
                manifest,
                dataset,
                str(snapshot.relative_to(case_dir)),
            )
            manifest["provenance"][dataset] = provenance
            save_manifest(manifest, manifest_path)

            log(f"[{ticker}] {dataset}: available")

        def prepare_prices():
            _, started = run_step(
                "prices.download",
                [ticker, quarter, "--as-of", cutoff_string],
                env=env,
            )

            data, content = fresh_json(
                case_dir / "price_action.json", started
            )

            if (
                data.get("ticker") != ticker
                or data.get("requested_as_of") != cutoff_string
                or data.get("benchmark") != "SPY"
                or not data.get("as_of")
                or date.fromisoformat(data["as_of"]) > cutoff
            ):
                raise ValueError("Price output context mismatch")

            return content, "price_action.json", {
                "source": data.get("source"),
                "actual_as_of": data["as_of"],
            }

        def prepare_transcript():
            # If retrieval raises DataUnavailable, selection is not run.
            run_step(
                "transcripts.retrieval",
                [ticker, quarter, "--source-quarter", prior],
                env=env,
            )

            _, started = run_step(
                "transcripts.selection",
                [
                    ticker,
                    quarter,
                    "--source-quarter", prior,
                    "--company-alias", alias,
                    "--as-of", cutoff_string,
                ],
                env=env,
            )

            metadata, _ = fresh_json(
                case_dir / "transcript_metadata.json", started
            )
            content = fresh_bytes(
                case_dir / "transcript.md", started
            )

            validation = metadata.get("validation", {})
            if (
                metadata.get("ticker") != ticker
                or metadata.get("target_quarter") != quarter
                or metadata.get("source_quarter") != prior
                or metadata.get("as_of") != cutoff_string
                or validation.get("status") != "passed_checks"
                or not validation.get("call_date")
                or date.fromisoformat(
                    validation["call_date"]
                ) > cutoff
                or metadata.get("cleaned_sha256")
                != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError("Transcript output context mismatch")

            return content, "transcript.md", metadata

        def prepare_consensus():
            # If retrieval fails, validation cannot use an old manifest.
            run_step(
                "consensus.retrieval",
                [
                    ticker,
                    quarter,
                    "--as-of", cutoff_string,
                    "--report-date", report_string,
                ],
                env=env,
            )

            _, started = run_step(
                "consensus.validation",
                [ticker, quarter],
                env=env,
            )

            data, content = fresh_json(
                case_dir / "consensus_proposed.json", started
            )

            if (
                data.get("ticker") != ticker
                or data.get("quarter") != quarter
                or data.get("as_of") != cutoff_string
                or data.get("next_earnings_date") != report_string
                or data.get("status") != "proposed"
                or not data.get("source_url")
                or not data.get("evidence")
                or data.get("revenue_estimate_usd_m") is None
                or data.get("eps_estimate") is None
                or not data.get("source_published_date")
                or date.fromisoformat(
                    data["source_published_date"]
                ) > cutoff
            ):
                raise ValueError("Consensus output context mismatch")

            if data.get("source_modified_date"):
                if date.fromisoformat(
                    data["source_modified_date"]
                ) > cutoff:
                    raise ValueError(
                        "Consensus source modified after cutoff"
                    )

            # Keep the proposal status, evidence and limitations unchanged.
            return content, "consensus.json", {
                "source_url": data["source_url"],
                "source_published_date": data["source_published_date"],
                "limitations": data.get("limitations", []),
            }

        collect("prices", prepare_prices)
        collect("transcript", prepare_transcript)
        collect("consensus", prepare_consensus)

        # No automatic authorization of an old guidance fixture.
        mark_unavailable(
            manifest,
            "guidance",
            DataStatus.UNSUPPORTED,
            "Structured guidance has not been registered for this run",
        )
        manifest["workflow_status"] = "prepared"
        save_manifest(manifest, manifest_path)

        if args.prepare_only:
            log(f"\nPreparation finished. Inspect: {manifest_path}")
            return

        usable = {"available", "partial"}
        has_core_data = any(
            manifest["datasets"][name]["status"] in usable
            for name in ("consensus", "transcript")
        )

        if not has_core_data:
            manifest["generation_status"] = "skipped"
            manifest["generation_reason"] = (
                "No usable consensus or transcript available"
            )
            save_manifest(manifest, manifest_path)
            log(
                "\nGeneration skipped: no usable core data. "
                f"See {manifest_path}"
            )
            return

        generation_env = env.copy()
        generation_env["MOCK"] = "1"

        manifest["generation_status"] = "running"
        save_manifest(manifest, manifest_path)

        note, _ = run_step(
            "pipeline",
            [
                ticker,
                quarter,
                "--manifest", str(manifest_path),
            ],
            env=generation_env,
            capture=True,
        )

        if not note or not note.strip():
            raise ValueError("Pipeline returned no preview")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output = OUTPUT_DIR / (
            f"{ticker.lower()}_{quarter.replace(' ', '_')}_"
            f"{manifest['run_id']}.md"
        )
        output.write_text(note, encoding="utf-8")

        manifest["generation_status"] = "completed"
        manifest["output_file"] = str(output)
        save_manifest(manifest, manifest_path)

        log(f"\nPreview saved: {output}")

    except (
        subprocess.CalledProcessError,
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
    ) as error:
        if manifest is not None:
            manifest["workflow_status"] = "aborted"
            manifest["error"] = str(error)
            save_manifest(manifest, manifest_path)

        parser.exit(1, f"\nWorkflow stopped: {error}\n")


if __name__ == "__main__":
    main()