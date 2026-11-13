import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
PROMPT_DIR = PACKAGE_DIR / "prompts"

def get_case_dir(ticker: str, quarter: str) -> Path:
    ticker = ticker.upper().strip()

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", ticker):
        raise ValueError(f"invalid ticker: {ticker!r}")

    match = re.fullmatch(
        r"\s*Q([1-4])\s*(?:FY)?\s*(20\d{2})\s*",
        quarter,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"invalid quarter: {quarter!r}")

    number, year = match.groups()
    return DATA_DIR / ticker / f"FY{year}_Q{number}"


def ensure_case_dir(ticker: str, quarter: str) -> Path:
    case_dir = get_case_dir(ticker, quarter)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir
