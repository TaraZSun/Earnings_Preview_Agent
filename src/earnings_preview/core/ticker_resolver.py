import re
import sys

import yfinance as yf


def resolve_ticker(raw: str, *, log=lambda message: None) -> dict:
    ticker = raw.upper().strip()

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", ticker):
        raise ValueError(f"Invalid ticker format: {raw!r}")

    log(f"Looking up company identity for {ticker}...")

    try:
        search = yf.Search(
            ticker,
            max_results=8,
            news_count=0,
            enable_fuzzy_query=True,
            timeout=20,
            raise_errors=True,
        )
        quotes = search.quotes
    except Exception as e:
        # A connection error must not be treated as an invalid symbol.
        raise RuntimeError(
            "Ticker lookup failed. Check connectivity or rate limits."
        ) from e

    candidates = []
    seen = set()

    for quote in quotes:
        # This earnings workflow supports company equities.
        if quote.get("quoteType") != "EQUITY":
            continue

        symbol = quote.get("symbol", "").upper()
        company = quote.get("longname") or quote.get("shortname")

        if not symbol or not company or symbol in seen:
            continue

        seen.add(symbol)
        candidates.append({
            "ticker": symbol,
            "company": company,
            "exchange": (
                quote.get("exchDisp")
                or quote.get("exchange")
                or "Unknown exchange"
            ),
        })

    exact = [
        item for item in candidates
        if item["ticker"] == ticker
    ]

    if len(exact) == 1:
        selected = exact[0]
        log(
            f"Matched: {selected['ticker']} — "
            f"{selected['company']} — {selected['exchange']}"
        )
        return selected

    if not candidates:
        raise ValueError(
            f"No equity match returned for {ticker}. "
            "Check the spelling or exchange suffix."
        )

    log(f"No unique exact equity match returned for {ticker}.")
    log("Possible matches:")

    for index, item in enumerate(candidates, start=1):
        log(
            f"{index}. {item['ticker']} — "
            f"{item['company']} — {item['exchange']}"
        )

    log("0. Cancel and enter another symbol")

    if not sys.stdin.isatty():
        raise ValueError(
            "An exact ticker is required in non-interactive mode."
        )

    while True:
        print("Select a number: ", end="", file=sys.stderr, flush=True)

        try:
            answer = input().strip()
        except EOFError:
            raise ValueError("Ticker selection cancelled") from None

        if answer == "0":
            raise ValueError("Ticker selection cancelled")

        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(candidates):
                selected = candidates[index - 1]
                log(
                    f"Selected: {selected['ticker']} — "
                    f"{selected['company']}"
                )
                return selected

        log("Please enter one of the listed numbers.")