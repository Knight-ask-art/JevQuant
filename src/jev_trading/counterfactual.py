"""Replay every decided BUY that has no outcome row yet.

    python -m jev_trading.counterfactual [--limit 400]

The cycle already replays a few signals per hour, so this is for draining the
backlog it has not reached: a restart drops the replays it had queued, and a
small venue of signals can otherwise sit unreplayed for days while every
cohort report says "insufficient". Read-only with respect to trading.
"""

from .services.counterfactual import main

if __name__ == "__main__":
    main()
