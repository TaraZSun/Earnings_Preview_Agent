import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from ..core.data_status import DataResult, DataStatus
from ..core.path import get_case_dir

DATASETS = ("prices", "consensus", "transcript", "guidance")
USABLE_STATUSES = {
    DataStatus.AVAILABLE,
    DataStatus.PARTIAL,
}


def create_manifest(ticker, quarter, as_of, report_date):
    ticker = ticker.upper().strip()
    date.fromisoformat(as_of)
    date.fromisoformat(report_date)

    manifest = {
        "version": 1,
        "run_id": uuid4().hex,
        "ticker": ticker,
        "quarter": quarter,
        "as_of": as_of,
        "report_date": report_date,
        "datasets": {
            name: DataResult(
                DataStatus.MISSING,
                reason="Not authorized for this run",
            ).to_dict()
            for name in DATASETS
        },
    }

    case_dir = get_case_dir(ticker, quarter)
    path = (
        case_dir
        / "runs"
        / manifest["run_id"]
        / "manifest.json"
    )

    save_manifest(manifest, path)
    return manifest, path


def save_manifest(manifest, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def mark_unavailable(manifest, dataset, status, reason):
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")

    status = DataStatus(status)
    if status in USABLE_STATUSES:
        raise ValueError("Use authorize_file for usable data")

    manifest["datasets"][dataset] = DataResult(
        status=status,
        reason=reason,
    ).to_dict()


def authorize_file(
    manifest,
    dataset,
    filename,
    *,
    status=DataStatus.AVAILABLE,
    reason="",
):
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")

    status = DataStatus(status)
    if status not in USABLE_STATUSES:
        raise ValueError("Only usable data can authorize a file")

    case_dir = get_case_dir(
        manifest["ticker"], manifest["quarter"]
    ).resolve()
    path = (case_dir / filename).resolve()

    if not path.is_relative_to(case_dir):
        raise ValueError("Input file is outside the case directory")

    content = path.read_bytes()
    if not content.strip():
        raise ValueError("Cannot authorize an empty input file")

    manifest["datasets"][dataset] = DataResult(
        status=status,
        reason=reason,
        file=str(path.relative_to(case_dir)),
        sha256=hashlib.sha256(content).hexdigest(),
    ).to_dict()


def load_manifest(path, *, ticker, quarter):
    manifest = json.loads(
        Path(path).read_text(encoding="utf-8")
    )

    if (
        manifest["version"] != 1
        or manifest["ticker"] != ticker.upper().strip()
        or manifest["quarter"] != quarter
    ):
        raise ValueError("Run manifest does not match requested case")

    date.fromisoformat(manifest["as_of"])
    date.fromisoformat(manifest["report_date"])

    for name in DATASETS:
        entry = manifest["datasets"][name]
        DataResult(
            status=DataStatus(entry["status"]),
            reason=entry["reason"],
            file=entry["file"],
            sha256=entry["sha256"],
        )

    return manifest


def read_authorized_file(manifest, dataset):
    entry = manifest["datasets"][dataset]
    status = DataStatus(entry["status"])

    if status not in USABLE_STATUSES:
        return None

    if not entry.get("file") or not entry.get("sha256"):
        raise ValueError("Authorized input lacks a file or fingerprint")

    case_dir = get_case_dir(
        manifest["ticker"], manifest["quarter"]
    ).resolve()
    path = (case_dir / entry["file"]).resolve()

    if not path.is_relative_to(case_dir):
        raise ValueError("Input file is outside the case directory")

    content = path.read_bytes()
    fingerprint = hashlib.sha256(content).hexdigest()

    if fingerprint != entry["sha256"]:
        raise ValueError(
            f"Authorized {dataset} file changed after registration"
        )

    return content