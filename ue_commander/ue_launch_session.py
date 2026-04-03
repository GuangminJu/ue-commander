"""
Launch session tracking for ue-commander.

This gives ue_launch / ue_status a stable session model so AI can reason about
which editor launch is active, whether it is ready, and which build it came from.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import UEConfig


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
class LaunchSession:
    launch_id: str
    status: str
    started_at: str
    finished_at: str | None
    editor_pid: int | None
    project_path: str
    command: str
    launched_by: str
    linked_build_id: str | None
    plugin_ready: bool
    phase: str
    log_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LaunchSessionStore:
    def __init__(self, cfg: UEConfig) -> None:
        self._cfg = cfg
        self._lock = threading.RLock()
        self._active_launch_id: str | None = None
        self._sessions: dict[str, LaunchSession] = {}
        self._storage_path = cfg.project_path.parent / "Saved" / "ue-commander" / "launch_sessions.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in raw.get("sessions", []):
            try:
                session = LaunchSession(**item)
            except TypeError:
                continue
            self._sessions[session.launch_id] = session
        self._active_launch_id = raw.get("active_launch_id")

    def _save(self) -> None:
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        payload = {
            "active_launch_id": self._active_launch_id,
            "sessions": [s.to_dict() for s in ordered[:20]],
        }
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _new_launch_id(self) -> str:
        return f"launch_{uuid.uuid4().hex[:8]}"

    def create_session(
        self,
        *,
        editor_pid: int | None,
        project_path: str,
        command: str,
        launched_by: str,
        linked_build_id: str | None,
        log_path: str,
    ) -> LaunchSession:
        with self._lock:
            session = LaunchSession(
                launch_id=self._new_launch_id(),
                status="starting",
                started_at=_now_iso(),
                finished_at=None,
                editor_pid=editor_pid,
                project_path=project_path,
                command=command,
                launched_by=launched_by,
                linked_build_id=linked_build_id,
                plugin_ready=False,
                phase="starting",
                log_path=log_path,
            )
            self._sessions[session.launch_id] = session
            self._active_launch_id = session.launch_id
            self._save()
            return session

    def get_session(self, launch_id: str) -> LaunchSession | None:
        with self._lock:
            return self._sessions.get(launch_id)

    def get_active_session(self) -> LaunchSession | None:
        with self._lock:
            if not self._active_launch_id:
                return None
            return self._sessions.get(self._active_launch_id)

    def get_last_session(self) -> LaunchSession | None:
        with self._lock:
            if not self._sessions:
                return None
            return max(
                self._sessions.values(),
                key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
            )

    def find_by_pid(self, pid: int | None) -> LaunchSession | None:
        if pid is None:
            return None
        with self._lock:
            for session in self._sessions.values():
                if session.editor_pid == pid and session.status != "closed":
                    return session
        return None

    def list_sessions(self, limit: int = 10) -> list[LaunchSession]:
        with self._lock:
            ordered = sorted(
                self._sessions.values(),
                key=lambda s: _parse_iso(s.started_at) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return ordered[:limit]

    def update_runtime(
        self,
        launch_id: str,
        *,
        editor_pid: int | None = None,
        launched_by: str | None = None,
        plugin_ready: bool | None = None,
        phase: str | None = None,
    ) -> LaunchSession | None:
        with self._lock:
            session = self._sessions.get(launch_id)
            if session is None:
                return None
            if editor_pid is not None:
                session.editor_pid = editor_pid
            if launched_by is not None:
                session.launched_by = launched_by
            if plugin_ready is not None:
                session.plugin_ready = plugin_ready
            if phase is not None:
                session.phase = phase
                if phase in {"starting", "loading", "ready"}:
                    session.status = phase
            if session.phase == "ready":
                session.plugin_ready = True
            self._save()
            return session

    def mark_closed(self, launch_id: str, *, phase: str = "closed") -> LaunchSession | None:
        with self._lock:
            session = self._sessions.get(launch_id)
            if session is None:
                return None
            session.finished_at = session.finished_at or _now_iso()
            session.phase = phase
            session.status = "closed" if phase == "closed" else "failed"
            session.plugin_ready = False
            if self._active_launch_id == launch_id:
                self._active_launch_id = None
            self._save()
            return session
