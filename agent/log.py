"""One line per thing that happened, on stdout.

The agent runs as a service more often than it runs in a terminal, and the
first question about it is always "is it still working", so every stage says
what it did in a line of its own. The shape is meercal's, so the two agents'
logs read the same way side by side.
"""

from __future__ import annotations

import logging
import sys
import time


def log(message: str, *, error: bool = False) -> None:
    stamp = time.strftime("%H:%M:%S")
    stream = sys.stderr if error else sys.stdout
    print(f"{stamp} {message}", file=stream, flush=True)


def setup() -> None:
    """Route library loggers (core.database's warnings, mostly) through the
    same timestamped format, on stderr, and keep chatty ones quiet."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # fastembed and huggingface_hub announce every file of a model download.
    for name in ("httpx", "huggingface_hub", "fastembed", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)
