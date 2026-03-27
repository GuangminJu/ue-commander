"""
HTTP client for the OhMyUnrealEngine plugin running inside UE editor.
All calls go through a single dispatch endpoint.
"""

import json
import urllib.request
import urllib.error
from typing import Any

DEFAULT_PORT = 9090
DEFAULT_TIMEOUT = 10  # seconds


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
    except urllib.error.URLError as e:
        return {
            "error": f"Cannot connect to UE plugin at {url}. "
                     "Is the editor running with OhMyUnrealEngine loaded?",
            "detail": str(e),
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
