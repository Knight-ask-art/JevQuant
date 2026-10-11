"""Read-only, credential-free decision-event capture and replay.

Only an allowlist of scalar fields is written. Never serialize model request
payloads, API responses, private account data or arbitrary metadata.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

VERSION = 1
ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
SOURCES = frozenset({"paper", "testnet", "live", "demo"})


def _utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _safe_text(value: Any, *, maximum: int = 100) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid recording field")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("control characters are not allowed")
    return value


def make_decision_frame(
    *,
    captured_at_utc: str,
    symbol: str,
    candle_close_utc: str,
    action: str,
    reason_code: str,
    signal_id: str,
    source_mode: str = "paper",
    confidence: Any = None,
) -> dict[str, Any]:
    """Build an explicitly allowlisted record with no secrets/metadata."""
    if action not in ACTIONS:
        raise ValueError("invalid action")
    if source_mode not in SOURCES:
        raise ValueError("invalid source mode")
    if confidence is not None:
        confidence = Decimal(str(confidence))
        if not confidence.is_finite() or not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("invalid confidence")
        confidence = str(confidence)
    return {
        "version": VERSION,
        "captured_at_utc": _utc_timestamp(captured_at_utc),
        "symbol": _safe_text(symbol, maximum=32),
        "candle_close_utc": _utc_timestamp(candle_close_utc),
        "action": action,
        "reason_code": _safe_text(reason_code, maximum=120),
        "signal_id": _safe_text(signal_id, maximum=64),
        "source_mode": source_mode,
        "confidence": confidence,
    }


def write_recording(path: Path, frames: Iterable[Mapping[str, Any]]) -> int:
    """Write a validated JSONL recording. No network or exchange access."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite recording: {path}")
    validated = [validate_frame(frame) for frame in frames]
    if not validated:
        raise ValueError("recording must contain at least one frame")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create avoids replacing existing recordings. Files are private
    # by default on POSIX; callers should still restrict parent permissions.
    with path.open("x", encoding="utf-8") as handle:
        for frame in validated:
            handle.write(json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return len(validated)


def validate_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, Mapping):
        raise ValueError("recording frame must be an object")
    allowed = {
        "version", "captured_at_utc", "symbol", "candle_close_utc",
        "action", "reason_code", "signal_id", "source_mode", "confidence",
    }
    if set(frame) != allowed or type(frame.get("version")) is not int or frame["version"] != VERSION:
        raise ValueError("invalid version or recording fields")
    # All record contents must pass the same strict validation as new frames.
    return make_decision_frame(
        captured_at_utc=frame["captured_at_utc"],
        symbol=frame["symbol"],
        candle_close_utc=frame["candle_close_utc"],
        action=frame["action"],
        reason_code=frame["reason_code"],
        signal_id=frame["signal_id"],
        source_mode=frame["source_mode"],
        confidence=frame["confidence"],
    )


def read_recording(path: Path) -> Iterator[dict[str, Any]]:
    """Stream validated frames in capture order; never execute actions."""
    saw_frame = False
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
                validated = validate_frame(frame)
            except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
                raise ValueError(f"invalid recording at line {line_number}") from exc
            saw_frame = True
            yield validated
    if not saw_frame:
        raise ValueError("recording is empty")
