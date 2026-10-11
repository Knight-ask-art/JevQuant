import json
from decimal import Decimal
import pytest
from jevquant.recording import (
    make_decision_frame, read_recording, validate_frame, write_recording,
)


def sample(**changes):
    values = {
        "captured_at_utc": "2026-10-11T16:00:00Z",
        "symbol": "BTC/USDT",
        "candle_close_utc": "2026-10-11T15:00:00Z",
        "action": "BUY",
        "reason_code": "JEV_DECISION_SUCCESS",
        "signal_id": "signal-0001",
        "source_mode": "paper",
        "confidence": Decimal("0.80"),
    }
    values.update(changes)
    return make_decision_frame(**values)


def test_recording_roundtrip_keeps_order_and_utc(tmp_path):
    path = tmp_path / "trace.jsonl"
    first = sample()
    second = sample(action="HOLD", confidence=None, signal_id="signal-0002")
    assert write_recording(path, [first, second]) == 2
    replay = list(read_recording(path))
    assert replay == [first, second]
    assert replay[0]["captured_at_utc"].endswith("+00:00")
    assert replay[1]["confidence"] is None
    with pytest.raises(FileExistsError):
        write_recording(path, [first])


@pytest.mark.parametrize("changes", [
    {"action": "TRANSFER"},
    {"source_mode": "unknown"},
    {"confidence": Decimal("NaN")},
    {"confidence": Decimal("Infinity")},
    {"confidence": Decimal("-0.01")},
    {"confidence": Decimal("1.01")},
    {"captured_at_utc": "2026-10-11T16:00:00"},
    {"reason_code": "OK\nsecret=abc"},
])
def test_recording_rejects_invalid_decisions(changes):
    with pytest.raises((ValueError, TypeError)):
        sample(**changes)


def test_reader_rejects_additional_sensitive_fields(tmp_path):
    frame = sample()
    frame["api_key"] = "should-not-be-recorded"
    path = tmp_path / "untrusted.jsonl"
    path.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        list(read_recording(path))


def test_writer_rejects_additional_sensitive_fields(tmp_path):
    frame = sample()
    frame["raw_response"] = {"Authorization": "Bearer secret"}
    with pytest.raises(ValueError):
        write_recording(tmp_path / "bad.jsonl", [frame])
    assert not (tmp_path / "bad.jsonl").exists()


def test_reader_rejects_unsupported_version_and_corrupt_lines(tmp_path):
    path = tmp_path / "wrong_version.jsonl"
    path.write_text(json.dumps({**sample(), "version": 2}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        list(read_recording(path))
    path.write_text("{ not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        list(read_recording(path))


def test_empty_recordings_fail(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        write_recording(tmp_path / "empty.jsonl", [])
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        list(read_recording(path))
