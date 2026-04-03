"""
Build session tracking for ue-commander.

This turns background compilation into a tracked session model that AI can
query reliably via build_id instead of relying on one global log file.
"""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import UEConfig
from .ue_build import BuildResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class BuildSession:
    build_id: str
    status: str
    started_at: str
    finished_at: str | None
    config: str
    target: str
    platform: str
    project_path: str
    command: str
    log_path: str
    pid: int | None
    exit_code: int | None
    result: str | None
    artifact_status: str
    warnings: int = 0
    errors: int = 0
    error_lines: list[str] = field(default_factory=list)
    warning_lines: list[str] = field(default_factory=list)
    output_tail: str = ""

    def to_dict(self, include_log: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_log:
            data.pop("output_tail", None)
        return data


class BuildSessionStore:
    def __init__(self, cfg: UEConfig) -> None:
        self._cfg = cfg
        self._lock = threading.RLock()
        self._active_build_id: str | None = None
        self._sessions: dict[str, BuildSession] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._storage_path = cfg.project_path.parent / "Saved" / "ue-commander" / "build_sessions.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = raw.get("sessions", [])
        for item in sessions:
            try:
                session = BuildSession(**item)
            except TypeError:
                continue
            self._sessions[session.build_id] = session
        self._active_build_id = raw.get("active_build_id")

    def _save(self) -> None:
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        payload = {
            "active_build_id": self._active_build_id,
            "sessions": [s.to_dict(include_log=True) for s in ordered[:20]],
        }
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _new_build_id(self) -> str:
        return f"build_{uuid.uuid4().hex[:8]}"

    def _new_log_path(self, build_id: str) -> str:
        return str(Path(tempfile.gettempdir()) / f"ue_compile_{build_id}.log")

    def has_running(self) -> bool:
        with self._lock:
            active = self.get_active_session(refresh=True)
            return active is not None and active.status in {"queued", "running"}

    def create_session(
        self,
        *,
        config: str,
        target: str,
        platform: str,
        project_path: str,
    ) -> BuildSession:
        with self._lock:
            build_id = self._new_build_id()
            session = BuildSession(
                build_id=build_id,
                status="queued",
                started_at=_now_iso(),
                finished_at=None,
                config=config,
                target=target,
                platform=platform,
                project_path=project_path,
                command="",
                log_path=self._new_log_path(build_id),
                pid=None,
                exit_code=None,
                result=None,
                artifact_status="unknown",
            )
            self._sessions[build_id] = session
            self._active_build_id = build_id
            self._save()
            return session

    def mark_running(self, build_id: str, thread: threading.Thread) -> None:
        with self._lock:
            session = self._sessions[build_id]
            session.status = "running"
            self._threads[build_id] = thread
            self._save()

    def finalize(self, build_id: str, result: BuildResult) -> BuildSession:
        with self._lock:
            session = self._sessions[build_id]
            session.finished_at = _now_iso()
            session.command = result.command
            session.exit_code = result.return_code
            session.result = "succeeded" if result.ok else "failed"
            session.status = session.result
            session.warnings = len(result.warnings)
            session.errors = len(result.errors)
            session.warning_lines = result.warnings[:10]
            session.error_lines = result.errors[:20]
            session.output_tail = result.output_tail[-20000:]
            session.artifact_status = self._infer_artifact_status(session, result)
            self._threads.pop(build_id, None)
            if self._active_build_id == build_id:
                self._active_build_id = None
            self._save()
            return session

    def mark_failed(self, build_id: str, message: str) -> BuildSession:
        with self._lock:
            session = self._sessions[build_id]
            session.finished_at = _now_iso()
            session.status = "failed"
            session.result = "failed"
            session.exit_code = session.exit_code if session.exit_code is not None else -1
            session.errors = 1
            session.error_lines = [message]
            session.artifact_status = "not_built"
            self._threads.pop(build_id, None)
            if self._active_build_id == build_id:
                self._active_build_id = None
            self._save()
            return session

    def get_session(self, build_id: str, refresh: bool = True) -> BuildSession | None:
        with self._lock:
            if refresh:
                self._refresh_session_locked(build_id)
            return self._sessions.get(build_id)

    def get_active_session(self, refresh: bool = True) -> BuildSession | None:
        with self._lock:
            if not self._active_build_id:
                return None
            if refresh:
                self._refresh_session_locked(self._active_build_id)
            return self._sessions.get(self._active_build_id)

    def get_last_session(self) -> BuildSession | None:
        with self._lock:
            if not self._sessions:
                return None
            latest = max(
                self._sessions.values(),
                key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
            )
            self._refresh_session_locked(latest.build_id)
            return latest

    def list_sessions(self, limit: int = 10) -> list[BuildSession]:
        with self._lock:
            for build_id in list(self._sessions):
                self._refresh_session_locked(build_id)
            ordered = sorted(
                self._sessions.values(),
                key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return ordered[:limit]

    def _refresh_session_locked(self, build_id: str) -> None:
        session = self._sessions.get(build_id)
        if session is None or session.status not in {"queued", "running"}:
            return
        thread = self._threads.get(build_id)
        if thread is not None and thread.is_alive():
            session.status = "running"
            return
        if session.finished_at is None:
            session.finished_at = _now_iso()
            session.status = "failed"
            session.result = "failed"
            if not session.error_lines:
                session.error_lines = ["Build thread ended without a final result."]
            session.errors = max(session.errors, len(session.error_lines))
            session.artifact_status = "unknown"
            self._threads.pop(build_id, None)
            if self._active_build_id == build_id:
                self._active_build_id = None
            self._save()

    def _infer_artifact_status(self, session: BuildSession, result: BuildResult) -> str:
        if not result.ok:
            return "not_built"

        output = result.output_tail
        if "Target is up to date" in output or "Result: Succeeded" in output:
            return "verified"

        started = _parse_iso(session.started_at)
        if started is None:
            return "built_but_unverified"

        candidates = [
            self._cfg.project_path.parent / "Binaries" / session.platform,
            self._cfg.engine_path / "Engine" / "Plugins" / "OhMyUnrealEngine" / "Binaries" / session.platform,
            self._cfg.engine_path / "Engine" / "Binaries" / session.platform,
        ]
        for root in candidates:
            if not root.exists():
                continue
            for item in root.iterdir():
                if not item.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(item.stat().st_mtime, tz=started.tzinfo)
                except OSError:
                    continue
                if mtime >= started:
                    return "verified"
        return "built_but_unverified"
