"""
HTTP client for the OhMyUnrealEngine plugin running inside UE editor.
All calls go through a single dispatch endpoint.
"""

import json
import os
import socket
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

DEFAULT_PORT = 9090
DEFAULT_TIMEOUT = 10  # seconds


def _get_crash_file_path() -> Path | None:
    """Get the path to the plugin's crash file (Saved/.ohmy_crash.json)."""
    project_path = os.environ.get("UE_PROJECT_PATH", "")
    if not project_path:
        return None
    return Path(project_path) / "Saved" / ".ohmy_crash.json"


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
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}
    except (socket.timeout, TimeoutError) as e:
        # Timeout — check crash file first, then probe once
        crash = read_crash_info()
        if crash is not None:
            return {
                "error": "UE editor crashed during MCP operation.",
                "crashed": True,
                "crash_info": crash,
            }
        # No crash file — probe once to distinguish hang from crash
        if is_plugin_available(port=port):
            return {
                "error": f"Request to {function_name} timed out after {timeout}s, "
                         "but UE is still running.",
                "crashed": False,
            }
        return {
            "error": "UE editor appears to have crashed or become unresponsive.",
            "crashed": True,
        }
    except (ConnectionRefusedError, ConnectionResetError,
            urllib.error.URLError, OSError) as e:
        # Connection refused/reset — UE is likely down, check crash file
        crash = read_crash_info()
        if crash is not None:
            return {
                "error": "UE editor crashed during MCP operation.",
                "crashed": True,
                "crash_info": crash,
            }
        return {
            "error": f"Cannot connect to UE plugin. "
                     "Is the editor running with OhMyUnrealEngine loaded?",
            "crashed": True,
        }
    except Exception as e:
        return {"error": str(e)}


def list_plugin_tools(port: int = DEFAULT_PORT, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Get the list of all available tools from the plugin."""
    url = f"{_base_url(port)}/api/tools"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.URLError as e:
        return {
            "error": f"Cannot connect to UE plugin at {url}.",
            "detail": str(e),
        }
    except Exception as e:
        return {"error": str(e)}


def is_plugin_available(port: int = DEFAULT_PORT) -> bool:
    """Quick check: is the plugin HTTP server reachable?"""
    result = list_plugin_tools(port=port, timeout=2)
    return "error" not in result
