import csv
import json

from ..core.path import get_case_dir

# Legacy summary command for the original Microsoft test case.
CASE_DIR = get_case_dir("MSFT", "Q4 2024")



def load_prices(ticker):
    path = CASE_DIR / f"{ticker}_daily.csv"
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    rows = sorted(
        (r for r in rows if r.get("Close")),
        key=lambda r: r["Date"],
    )

    if len(rows) < 64:
        raise ValueError(f"{ticker}: at least 64 trading days are required")

    if rows[-1]["Date"] != "2024-07-29":
        raise ValueError(f"{ticker}: the last date must be 2024-07-29")

    return rows


def price_return(rows, sessions):
    # 21 / 63 trading days approximate one / three months.
    last = float(rows[-1]["Close"])
    previous = float(rows[-sessions - 1]["Close"])
    return round((last / previous - 1) * 100, 2)


def main():
    msft = load_prices("MSFT")
    spy = load_prices("SPY")

    summary = {
        "ticker": "MSFT",
        "as_of": msft[-1]["Date"],
        "last_close": round(float(msft[-1]["Close"]), 2),
        "return_1m_pct": price_return(msft, 21),
        "return_3m_pct": price_return(msft, 63),
        "benchmark": "SPY",
        "benchmark_return_3m_pct": price_return(spy, 63),
        "high_52w": round(max(float(r["High"]) for r in msft), 2),
        "low_52w": round(min(float(r["Low"]) for r in msft), 2),
        "avg_daily_volume_3m": round(
            sum(float(r["Volume"]) for r in msft[-63:]) / 63
        ),
        "return_basis": "Close, unadjusted; excludes dividend income",
        "source": "local CSV downloaded from Yahoo Finance via yfinance",
    }

    output = CASE_DIR / "price_action.json"
    output.write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
