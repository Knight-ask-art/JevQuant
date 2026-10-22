"""One logging setup for every entry point that runs the service.

The problem this fixes was invisible until it mattered: every module logs
through ``jev_trading.*`` loggers, but nothing ever attached a handler to that
family, so ``logger.info("counterfactual backfill wrote N outcomes")`` and
``logger.exception("auto cycle failed")`` went nowhere. The only lines in the
container log were uvicorn's access log, which is why "the backfill wrote
nothing" and "the backfill crashed" looked identical from outside.

Uvicorn configures only its own ``uvicorn*`` loggers. The ``jev_trading``
logger is a sibling of those, so it needs its own handler to reach the
container's stderr.

Rules this keeps:

- **stderr, not stdout**, matching uvicorn's own output so ``docker logs``
  interleaves them in order.
- **The format is fixed.** A handler never prints a record's arguments beyond
  what the call site passed, and nobody should pass a secret in the first
  place; see the no-secrets invariant in ``CLAUDE.md``.
- **Idempotent.** Called from ``serve`` and from ``python -m`` entry points; a
  second call must not double every line.
"""

from __future__ import annotations

import logging
import os

LOGGER_NAME = "jev_trading"
#: What a handler installed by this module has, so a second call recognises it.
_HANDLER_NAME = "jev_trading.console"


def configure_logging(level: str | int | None = None) -> None:
    """Send ``jev_trading.*`` records to stderr once, at ``JEV_LOG_LEVEL``."""

    resolved = logging.getLogger(LOGGER_NAME)
    resolved_level = _level(level)
    for handler in resolved.handlers:
        if getattr(handler, "name", None) == _HANDLER_NAME:
            resolved.setLevel(resolved_level)
            return

    handler = logging.StreamHandler()  # stderr
    handler.name = _HANDLER_NAME
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    resolved.addHandler(handler)
    resolved.setLevel(resolved_level)
    # Do not climb to the root logger: in a container the root is uvicorn's,
    # and a second handler there would print every record twice.
    resolved.propagate = False


def _level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    name = (level or os.environ.get("JEV_LOG_LEVEL") or "INFO").upper()
    resolved = logging.getLevelName(name)
    # An unknown name yields the string "Level XYZ"; fall back rather than
    # making the logger silently drop everything.
    return resolved if isinstance(resolved, int) else logging.INFO
