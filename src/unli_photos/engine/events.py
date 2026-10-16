"""Typed engine events consumed by frontends (TUI, headless listener)."""

from __future__ import annotations

from dataclasses import dataclass


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
    queued: int


@dataclass(frozen=True)
class PrecheckIssue(EngineEvent):
    message: str


@dataclass(frozen=True)
class LogMessage(EngineEvent):
    level: str
    message: str
