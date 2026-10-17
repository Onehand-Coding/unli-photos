"""Typed engine events consumed by frontends (TUI, headless listener, shared log)."""

from __future__ import annotations

import json
import typing
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EngineEvent:
    pass


@dataclass(frozen=True)
class EngineStarted(EngineEvent):
    watch_dirs: tuple[str, ...]


@dataclass(frozen=True)
class EngineStopped(EngineEvent):
    pass


@dataclass(frozen=True)
class FileDetected(EngineEvent):
    path: str
    size: int


@dataclass(frozen=True)
class UploadSkipped(EngineEvent):
    path: str
    reason: str


@dataclass(frozen=True)
class UploadStarted(EngineEvent):
    path: str
    size: int


@dataclass(frozen=True)
class UploadSucceeded(EngineEvent):
    path: str
    size: int
    duration: float
    media_key: str | None


@dataclass(frozen=True)
class UploadFailed(EngineEvent):
    path: str
    duration: float
    error: str | None
    attempts: int


@dataclass(frozen=True)
class RescanStarted(EngineEvent):
    pass


@dataclass(frozen=True)
class RescanDone(EngineEvent):
    scanned: int
    queued: int


@dataclass(frozen=True)
class PrecheckIssue(EngineEvent):
    message: str


@dataclass(frozen=True)
class LogMessage(EngineEvent):
    level: str
    message: str


_JSON_INDEX: dict[str, type[EngineEvent]] = {
    cls.__name__: cls
    for cls in (
        EngineStarted,
        EngineStopped,
        FileDetected,
        UploadSkipped,
        UploadStarted,
        UploadSucceeded,
        UploadFailed,
        RescanStarted,
        RescanDone,
        PrecheckIssue,
        LogMessage,
    )
}


def to_json(event: EngineEvent) -> str:
    """JSON line for the shared cross-process event log (tuples become lists)."""
    payload: dict[str, object] = {"kind": type(event).__name__}
    for key, value in asdict(event).items():
        payload[key] = list(value) if isinstance(value, tuple) else value
    return json.dumps(payload)


def from_json(line: str) -> EngineEvent:
    """Rebuild an event from a to_json() line; ValueError on unknown kind."""
    raw = json.loads(line)
    kind = raw.pop("kind", None)
    cls = _JSON_INDEX.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ValueError(f"unknown event kind: {kind!r}")
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, object] = {}
    for key, value in raw.items():
        if (
            isinstance(value, list)
            and typing.get_origin(hints.get(key, None)) is tuple
        ):
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)  # type: ignore[return-value]
