from dataclasses import dataclass


@dataclass(slots=True)
class BridgeState:
    """Normalized state for the UE editor bridge."""

    state: str
    plugin_ready: bool
    game_thread_responsive: bool
    crash_info: dict | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        result = {
            "state": self.state,
            "plugin_ready": self.plugin_ready,
            "game_thread_responsive": self.game_thread_responsive,
        }
        if self.detail:
            result["detail"] = self.detail
        if self.crash_info is not None:
            result["crash_info"] = self.crash_info
        return result
