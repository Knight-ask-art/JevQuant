"""The service's own log records reach the container log, exactly once.

Before this, every ``jev_trading.*`` record was dropped: the backfill's success
line, the auto-cycle failure traceback, the abandoned-signal warning. That is
not a cosmetic gap -- it made "wrote nothing" and "crashed" indistinguishable
in the only place an operator looks.
"""

from __future__ import annotations

import logging

import pytest

from jev_trading.logging_setup import LOGGER_NAME, configure_logging


@pytest.fixture
def clean_logger():
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in saved[0]:
            logger.addHandler(handler)
        logger.setLevel(saved[1])
        logger.propagate = saved[2]


def test_a_record_reaches_stderr(clean_logger, capsys):
    configure_logging("INFO")

    logging.getLogger("jev_trading.test").info("backfill wrote %s outcomes", 3)

    captured = capsys.readouterr()
    assert "backfill wrote 3 outcomes" in captured.err
    assert captured.out == ""


def test_calling_twice_does_not_duplicate(clean_logger, capsys):
    """serve, a CLI entry point and a test can all call this in one process."""

    configure_logging("INFO")
    configure_logging("INFO")
    logging.getLogger("jev_trading.test").info("once")

    assert capsys.readouterr().err.count("once") == 1
    ours = [h for h in clean_logger.handlers if getattr(h, "name", None) == "jev_trading.console"]
    assert len(ours) == 1


def test_the_record_does_not_climb_to_the_root_logger(clean_logger):
    """In a container the root has uvicorn's handler; doubling would reorder."""

    configure_logging("INFO")

    assert clean_logger.propagate is False


def test_the_level_can_be_raised(clean_logger, capsys):
    configure_logging("WARNING")
    logging.getLogger("jev_trading.test").info("hidden")
    logging.getLogger("jev_trading.test").warning("shown")

    err = capsys.readouterr().err
    assert "hidden" not in err
    assert "shown" in err


def test_an_unknown_level_falls_back_instead_of_dropping_everything(
    clean_logger, monkeypatch, capsys
):
    monkeypatch.setenv("JEV_LOG_LEVEL", "LOUD")
    configure_logging()
    logging.getLogger("jev_trading.test").info("still visible")

    assert "still visible" in capsys.readouterr().err

