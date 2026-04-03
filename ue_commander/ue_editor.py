from __future__ import annotations

"""
HTTP client for the OhMyUnrealEngine plugin running inside UE editor.
Falls back to a file bridge when the HTTP listener is present but unresponsive.
"""

import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_PORT = 9090
DEFAULT_TIMEOUT = 10  # seconds


def _get_crash_file_path() -> Path | None:
    """Get the path to the plugin's crash file (Saved/.ohmy_crash.json)."""
    try:
        from .config import find_uproject

        uproject = find_uproject()
        return uproject.parent / "Saved" / ".ohmy_crash.json"
    except Exception:
        return None


def read_crash_info() -> dict | None:
    """Read and return crash info if the crash file exists. Returns None if no crash."""
    path = _get_crash_file_path()
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return {"error": "Crash file exists but could not be parsed", "path": str(path)}


def clear_crash_info() -> None:
    """Delete the crash file (call after user has seen the crash info)."""
    path = _get_crash_file_path()
    if path is not None and path.exists():
        path.unlink(missing_ok=True)


def _base_url(port: int = DEFAULT_PORT) -> str:
    return f"http://localhost:{port}"


def _project_saved_candidates() -> list[Path]:
    """Return candidate Saved directories for the active UE project."""
    import os

    candidates: list[Path] = []

    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if "unrealeditor" not in name:
                continue
            for arg in (proc.info.get("cmdline") or []):
                if isinstance(arg, str) and arg.endswith(".uproject"):
                    candidates.append(Path(arg).parent / "Saved")
            break
    except Exception:
        pass

    env_path = os.environ.get("UE_PROJECT_PATH")
    if env_path:
        p = Path(env_path)
        candidates.append((p if p.is_dir() else p.parent) / "Saved")

    try:
        from .config import find_uproject

        candidates.append(find_uproject().parent / "Saved")
    except Exception:
        pass

    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(resolved)
    return deduped


def _get_ipc_dir() -> Path | None:
    """Get the Saved/.ohmy_ipc directory for the active UE project."""
    candidates = _project_saved_candidates()
    for saved_dir in candidates:
        ipc_dir = saved_dir / ".ohmy_ipc"
        if ipc_dir.exists():
            return ipc_dir
    if candidates:
        return candidates[0] / ".ohmy_ipc"
    return None


def _read_file_bridge_state() -> dict | None:
    ipc_dir = _get_ipc_dir()
    if ipc_dir is None:
        return None

    state_path = ipc_dir / "state.json"
    if not state_path.exists():
        return None

    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _file_bridge_available(max_age_seconds: float = 5.0) -> bool:
    state = _read_file_bridge_state()
    if not isinstance(state, dict) or state.get("ok") is not True:
        return False

    updated_at = state.get("updated_at")
    if not isinstance(updated_at, str):
        return False

    try:
        from datetime import datetime

        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False

    return (time.time() - updated) <= max_age_seconds


def _request_file_bridge(
    payload: dict[str, Any],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: float = 0.1,
) -> dict:
    ipc_dir = _get_ipc_dir()
    if ipc_dir is None:
        return {"error": "Cannot locate Saved/.ohmy_ipc for the active UE project."}

    ipc_dir.mkdir(parents=True, exist_ok=True)

    request_id = payload.setdefault("id", uuid.uuid4().hex)
    request_path = ipc_dir / f"request_{request_id}.json"
    response_path = ipc_dir / f"response_{request_id}.json"

    try:
        request_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to write file bridge request: {exc}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        if response_path.exists():
            try:
                body = response_path.read_text(encoding="utf-8")
                response = json.loads(body)
                if isinstance(response, dict):
                    response.setdefault("transport", "file")
                return response
            except (json.JSONDecodeError, OSError) as exc:
                return {"error": f"Failed to read file bridge response: {exc}", "transport": "file"}
            finally:
                request_path.unlink(missing_ok=True)
                response_path.unlink(missing_ok=True)
        time.sleep(poll_interval)

    request_path.unlink(missing_ok=True)
    return {"error": f"File bridge request timed out after {timeout}s.", "transport": "file"}


def _read_auth_token() -> str | None:
    """Read the auth token from the plugin's token file (Saved/.ohmy_token).

    Checks multiple locations in priority order:
    1. The running UE project's Saved dir (auto-detected from process cmdline)
    2. UE_PROJECT_PATH env var
    3. find_uproject() from CWD
    """
    candidates = [saved_dir / ".ohmy_token" for saved_dir in _project_saved_candidates()]

    for token_path in candidates:
        try:
            if token_path.exists():
                text = token_path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def _auth_headers() -> dict[str, str]:
    """Return headers dict including auth token if available."""
    headers = {"Content-Type": "application/json"}
    token = _read_auth_token()
    if token:
        headers["X-OhMy-Token"] = token
    return headers


def call_plugin(function_name: str, port: int = DEFAULT_PORT, timeout: int = DEFAULT_TIMEOUT, **params) -> dict:
    """
    Call a function on the UE plugin via HTTP.
    Returns parsed JSON response or error dict.
    """
    url = f"{_base_url(port)}/api/call"
    payload = {"function": function_name}
    if params:
        payload["params"] = params

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=_auth_headers(),
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}
    except (socket.timeout, TimeoutError):
        if _file_bridge_available():
            return _request_file_bridge(
                {"op": "call", "function": function_name, "params": params},
                timeout=timeout,
            )

        crash = read_crash_info()
        if crash is not None:
            return {
                "error": "UE editor crashed during MCP operation.",
                "crashed": True,
                "crash_info": crash,
            }
        if is_plugin_available(port=port):
            return {
                "error": f"Request to {function_name} timed out after {timeout}s, but UE is still running.",
                "crashed": False,
            }
        return {
            "error": "UE editor appears to have crashed or become unresponsive.",
            "crashed": True,
        }
    except (ConnectionRefusedError, ConnectionResetError, urllib.error.URLError, OSError):
        if _file_bridge_available():
            return _request_file_bridge(
                {"op": "call", "function": function_name, "params": params},
                timeout=timeout,
            )

        crash = read_crash_info()
        if crash is None:
            time.sleep(1)
            crash = read_crash_info()
        if crash is not None:
            return {
                "error": "UE editor crashed during MCP operation.",
                "crashed": True,
                "crash_info": crash,
            }
        return {
            "error": "Cannot connect to UE plugin. Is the editor running with OhMyUnrealEngine loaded?",
            "crashed": True,
        }
    except Exception as e:
        return {"error": str(e)}


def list_plugin_tools(
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
    *,
    detail: str | None = None,
) -> dict:
    """Get the list of available tools from the plugin.

    detail=None returns the lightweight summary index.
    detail="full" returns the legacy/full schema payload.
    """
    url = f"{_base_url(port)}/api/tools"
    if detail:
        url = f"{url}?detail={detail}"
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        if _file_bridge_available():
            return _request_file_bridge(
                {"op": "list_tools", "include_schemas": detail == "full"},
                timeout=timeout,
            )
        return {"error": f"Cannot connect to UE plugin at {url}."}
    except Exception as e:
        return {"error": str(e)}


def list_plugin_tool_schemas(
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
    *,
    name: str | None = None,
) -> dict:
    """Get full schema metadata for one plugin tool or all plugin tools."""
    url = f"{_base_url(port)}/api/tools/schema"
    if name:
        from urllib.parse import quote

        url = f"{url}?name={quote(name)}"
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        if _file_bridge_available():
            return _request_file_bridge(
                {"op": "list_tool_schemas", "name": name or ""},
                timeout=timeout,
            )
        return {"error": f"Cannot connect to UE plugin at {url}."}
    except Exception as e:
        return {"error": str(e)}


def call_plugin_batch(calls: list[dict], port: int = DEFAULT_PORT, timeout: int = 120) -> dict:
    """
    Execute multiple plugin calls in a single HTTP request.
    Each call is {"function": "...", "params": {...}}.
    All calls run in one undo transaction.
    """
    url = f"{_base_url(port)}/api/batch"
    data = json.dumps(calls).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=_auth_headers(),
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}
    except (socket.timeout, TimeoutError, ConnectionRefusedError, ConnectionResetError, urllib.error.URLError, OSError):
        if _file_bridge_available():
            return _request_file_bridge({"op": "batch", "calls": calls}, timeout=timeout)

        crash = read_crash_info()
        if crash is None:
            time.sleep(1)
            crash = read_crash_info()
        if crash is not None:
            return {"error": "UE crashed during batch call.", "crashed": True, "crash_info": crash}
        return {"error": f"Batch call failed or timed out after {timeout}s.", "crashed": False}
    except Exception as e:
        return {"error": str(e)}


def ping_plugin(port: int = DEFAULT_PORT, timeout: int = 5) -> dict:
    """
    Lightweight ping — responds on HTTP thread without game thread dispatch.
    Falls back to the file bridge when HTTP is unresponsive.
    """
    url = f"{_base_url(port)}/api/ping"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except Exception:
        if _file_bridge_available():
            return _request_file_bridge({"op": "ping"}, timeout=timeout)
        return {"ok": False}


def is_plugin_available(port: int = DEFAULT_PORT) -> bool:
    """Quick check: is the plugin control plane reachable?"""
    result = ping_plugin(port=port)
    return result.get("ok", False)
