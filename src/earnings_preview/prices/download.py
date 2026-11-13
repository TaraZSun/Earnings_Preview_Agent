import argparse
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf
from curl_cffi.requests.exceptions import RequestException
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from ..core.cli import run_cli
from ..core.data_status import DataStatus, DataUnavailable
from ..core.path import ensure_case_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("quarter")
    parser.add_argument(
    "--as-of",
    help="Data cutoff date (YYYY-MM-DD); defaults to today in Asia/Shanghai",
    )
    parser.add_argument("--benchmark", default="SPY")
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    benchmark = args.benchmark.upper().strip()
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            parser.error("--as-of must be a valid date in YYYY-MM-DD format")
    else:
        as_of = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    print(f"Requested data cutoff: {as_of.isoformat()}", flush=True)

    case_dir = ensure_case_dir(ticker, args.quarter)

    # Include the cutoff date; yfinance's end date is exclusive.
    start = as_of - timedelta(weeks=52)
    end = as_of + timedelta(days=1)

    frames = {}

    for symbol in dict.fromkeys([ticker, benchmark]):
        print(f"Downloading {symbol}...", flush=True)

        try:
            frame = yf.Ticker(symbol).history(
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=True,
            )
        except (RequestException, YFRateLimitError) as e:
            raise DataUnavailable(
                DataStatus.FAILED,
                f"Price request failed for {symbol}: {type(e).__name__}",
            ) from e
        except YFPricesMissingError as e:
            raise DataUnavailable(
                DataStatus.MISSING,
                f"No usable price history returned for {symbol}",
            ) from e

        if frame.empty:
            raise DataUnavailable(
                DataStatus.MISSING,
                f"No price data returned for {symbol}",
            )

        frame.index = frame.index.strftime("%Y-%m-%d")
        frame.index.name = "Date"
        frame = frame.sort_index()

        # Enforce the requested historical cutoff.
        frame = frame.loc[
            (frame.index >= start.isoformat())
            & (frame.index <= as_of.isoformat())
        ]

        if frame.empty:
            raise DataUnavailable(
                DataStatus.MISSING,
                f"No price data returned for {symbol}",
            )
        frames[symbol] = frame

    stock = frames[ticker]
    market = frames[benchmark]

    dates = stock["Close"].dropna().index.intersection(
        market["Close"].dropna().index
    ).sort_values()

    if len(dates) < 64:
        raise DataUnavailable(
            DataStatus.MISSING,
            "At least 64 overlapping trading days are required",
        )

    stock_close = stock.loc[dates, "Close"]
    market_close = market.loc[dates, "Close"]

    def price_return(close, sessions):
        return round(
            (float(close.iloc[-1]) / float(close.iloc[-sessions - 1]) - 1)
            * 100,
            2,
        )

    summary = {
        "ticker": ticker,
        "requested_as_of": as_of.isoformat(),
        "as_of": dates[-1],
        "last_close": round(float(stock_close.iloc[-1]), 2),
        "return_1m_pct": price_return(stock_close, 21),
        "return_3m_pct": price_return(stock_close, 63),
        "benchmark": benchmark,
        "benchmark_return_3m_pct": price_return(market_close, 63),
        "high_52w": round(float(stock["High"].max()), 2),
        "low_52w": round(float(stock["Low"].min()), 2),
        "avg_daily_volume_3m": round(
            float(stock.loc[dates[-63:], "Volume"].mean())
        ),
        "return_basis": "Close, unadjusted; excludes dividend income",
        "return_windows_trading_days": {
            "1m": 21,
            "3m": 63,
        },
        "source": "Yahoo Finance via yfinance",
    }

    # Save only after both downloads and the basic checks succeed.
    for symbol, frame in frames.items():
        frame.to_csv(case_dir / f"{symbol}_daily.csv")

    (case_dir / "price_action.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    metadata = {
        "source": "Yahoo Finance via yfinance",
        "ticker": ticker,
        "benchmark": benchmark,
        "start_inclusive": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "requested_as_of": as_of.isoformat(),
        "last_common_trading_date": dates[-1],
        "auto_adjust": False,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    (case_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    print(f"Saved files to {case_dir}")


if __name__ == "__main__":
    run_cli(main)