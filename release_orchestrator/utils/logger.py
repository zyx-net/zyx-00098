"""Structured logging utility for the release orchestrator.

Provides a singleton logger that writes to both stdout and an in-memory
buffer so logs can be persisted to disk as part of each execution's
snapshot.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LogEntry:
    def __init__(self, timestamp: str, level: str, module: str, message: str,
                 extra: Optional[Dict[str, Any]] = None):
        self.timestamp = timestamp
        self.level = level
        self.module = module
        self.message = message
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "module": self.module,
            "message": self.message,
            "extra": self.extra,
        }


class OrchestratorLogger:
    """Structured logger with in-memory log capture."""

    def __init__(self, name: str = "release_orchestrator", verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self._buffer: StringIO = StringIO()
        self._entries: List[LogEntry] = []
        self._console = sys.stdout

    def _log(self, level: str, module: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        ts = _now_iso()
        entry = LogEntry(timestamp=ts, level=level, module=module, message=message, extra=extra)
        self._entries.append(entry)
        line = self._format_entry(entry)
        self._console.write(line + "\n")
        self._console.flush()
        self._buffer.write(line + "\n")

    def _format_entry(self, entry: LogEntry) -> str:
        parts = [
            f"[{entry.timestamp}]",
            f"[{entry.level:7s}]",
            f"[{entry.module}]",
            entry.message,
        ]
        if entry.extra:
            parts.append(json.dumps(entry.extra, ensure_ascii=False))
        return " ".join(parts)

    def debug(self, module: str, message: str, **extra: Any) -> None:
        if self.verbose:
            self._log("DEBUG", module, message, extra or None)

    def info(self, module: str, message: str, **extra: Any) -> None:
        self._log("INFO", module, message, extra or None)

    def warning(self, module: str, message: str, **extra: Any) -> None:
        self._log("WARNING", module, message, extra or None)

    def error(self, module: str, message: str, **extra: Any) -> None:
        self._log("ERROR", module, message, extra or None)

    def critical(self, module: str, message: str, **extra: Any) -> None:
        self._log("CRITICAL", module, message, extra or None)

    def get_text(self) -> str:
        return self._buffer.getvalue()

    def get_entries(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self._entries]

    def clear(self) -> None:
        self._entries.clear()
        self._buffer = StringIO()


_LOGGER: Optional[OrchestratorLogger] = None


def get_logger(verbose: bool = False, reset: bool = False) -> OrchestratorLogger:
    """Return the global logger instance, creating it on first call.

    When reset=True, the existing logger's buffers and entry list are
    cleared (and verbose flag is updated), but the *same* object is
    returned so that module-level LOG references remain valid.
    """
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = OrchestratorLogger(verbose=verbose)
    else:
        _LOGGER.verbose = _LOGGER.verbose or verbose
        if reset:
            _LOGGER.clear()
            _LOGGER.verbose = verbose
    return _LOGGER
