import json
import sys

from .data_status import DataResult, DataUnavailable

DATA_UNAVAILABLE_EXIT_CODE = 10


def run_cli(main):
    try:
        main()
    except DataUnavailable as error:
        result = DataResult(
            status=error.status,
            reason=error.reason,
        )
        print(
            "DATA_UNAVAILABLE:"
            + json.dumps(result.to_dict()),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(DATA_UNAVAILABLE_EXIT_CODE) from None