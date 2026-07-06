from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..services import computer
from .base import ToolResult, ToolboxHelpers


class ComputerTools(ToolboxHelpers):
    """Screen capture and desktop input.

    screen_capture results carry meta.screenshot_path; the agent runner
    collects those and attaches the image to the next model turn, closing the
    see-act-verify loop. meta.screenshot_url is where the browser UI fetches
    the same image (chat thumbnail, gallery).
    """

    def _screen_capture(self, monitor: int = 1) -> ToolResult:
        info = computer.screen_capture(monitor)
        path = str(info.get("path") or "")
        thumb = str(info.get("thumbnail_path") or "")
        grid = str(info.get("grid_path") or "")
        full_url = self._artifact_url(path)
        # Inline transcript uses the small thumbnail (falls back to full when no
        # thumbnail was generated); the model and gallery use the full image.
        inline_url = self._artifact_url(thumb) if thumb else full_url
        # The model sees the GRID-annotated image (coordinate labels aid
        # clicking); the user gallery keeps the clean screenshot.
        model_image = grid or path
        width, height = info.get("width"), info.get("height")
        return ToolResult(
            True,
            "screen_capture",
            (
                f"Captured the primary monitor ({width}x{height}). The attached image has a "
                "cyan coordinate grid overlaid: vertical/horizontal lines every 100px labeled "
                "with their x/y pixel value. To click a target, read the nearest gridlines to "
                "estimate its (x, y) and click that coordinate. Top-left is (0,0), bottom-right "
                f"is ({width},{height})."
            ),
            {
                **info,
                "screenshot_path": model_image,
                "screenshot_url": inline_url,
                "screenshot_full_url": full_url,
            },
        )

    def _artifact_url(self, path: str) -> str:
        try:
            rel = Path(path).resolve().relative_to(self._artifacts_root().resolve())
            return "/api/artifacts/" + rel.as_posix()
        except (ValueError, OSError):
            return "/api/artifacts/screenshots/" + Path(path).name

    def _computer_action(self, tool_name: str, action: Any) -> ToolResult:
        result = action()
        return ToolResult(True, tool_name, json.dumps(result), dict(result))

    def _open_app(self, name: str) -> ToolResult:
        result = computer.open_app(name)
        return ToolResult(
            True,
            "open_app",
            f"Launched {result.get('app')} (pid {result.get('pid')}). "
            "Take a screen_capture to see its window before interacting.",
            dict(result),
        )

    def _list_windows(self) -> ToolResult:
        windows = computer.list_windows()
        if not windows:
            return ToolResult(True, "list_windows", "No titled windows found.", {"windows": []})
        lines = [f"- {window.get('title')}" for window in windows[:40]]
        return ToolResult(True, "list_windows", "\n".join(lines), {"windows": windows})
