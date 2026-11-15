"""Internal fresh-process entry point. Credentials are inherited, never serialized."""

import contextlib
import io
import json
import logging
import sys

from parkingbreaker.bench.cli import WorkItem, execute_work


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        item = WorkItem(**json.loads(sys.stdin.read()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute_work(item)
    except Exception:
        # Never echo provider exceptions, environment, input, or tracebacks.
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
