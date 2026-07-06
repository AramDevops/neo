from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from ..config import ARTIFACTS_DIR


SCREENSHOT_DIR = ARTIFACTS_DIR / "screenshots"


def _ensure_dpi_awareness() -> None:
    """Make this process DPI-aware so screenshot pixels equal click pixels.

    Without it, on a scaled display (e.g. 150%) mss captures physical pixels
    while pyautogui clicks in virtualized logical pixels, so every coordinate
    read off the screenshot lands in the wrong place. Set once, best-effort;
    per-monitor-v2 is ideal, plain system-DPI-aware is the fallback.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_ensure_dpi_awareness()

# Keys accepted by computer_key, mapped to pyautogui names.
KEY_ALIASES = {
    "esc": "esc", "escape": "esc", "enter": "enter", "return": "enter", "tab": "tab",
    "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "win": "win", "windows": "win", "cmd": "win", "super": "win",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}


def _pyautogui():
    try:
        import pyautogui  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError(
            "Computer input control requires the pyautogui package. "
            "Install it with: pip install pyautogui"
        ) from exc
    except Exception as exc:  # pragma: no cover - installed but failed to init
        # Do NOT claim pyautogui is missing when it is installed; surface the
        # real reason (e.g. no interactive desktop session / display) so the
        # failure is actionable instead of a misleading install hint.
        raise RuntimeError(f"Computer input control is unavailable: {exc}") from exc
    try:
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
    except Exception:
        pass
    return pyautogui


def _write_thumbnail(source: Path, width: int, height: int, max_width: int = 480) -> str:
    """Save a small thumbnail next to the full screenshot.

    Inline transcript thumbnails must not decode full-resolution PNGs in the
    browser; several multi-thousand-pixel captures decoded to hundreds of MB
    of bitmap and crashed the tab with Out-of-Memory. The full image is still
    kept for the model and the gallery lightbox.
    """
    if not width or width <= max_width:
        return ""
    try:
        from PIL import Image  # type: ignore

        thumb_path = source.with_name(source.stem + ".thumb.png")
        with Image.open(source) as image:
            ratio = max_width / float(width)
            image.thumbnail((max_width, max(1, int(height * ratio))))
            image.save(thumb_path, "PNG")
        return str(thumb_path)
    except Exception:
        return ""


def _draw_coordinate_grid(source: Path, width: int, height: int, step: int = 100) -> str:
    """Overlay a labeled coordinate grid on the screenshot the model sees.

    A weak vision model can't reliably invent pixel coordinates ("click the
    Control Panel" -> a guessed screen-center). With gridlines every `step`
    pixels labeled with their x/y value, the model reads the target's real
    coordinates off the image instead of guessing. Because we capture the
    primary monitor 1:1, those coordinates map directly to pyautogui clicks.
    """
    if not width or not height:
        return ""
    try:
        from PIL import Image, ImageDraw  # type: ignore

        grid_path = source.with_name(source.stem + ".grid.png")
        with Image.open(source).convert("RGB") as base:
            draw = ImageDraw.Draw(base)
            major = (0, 200, 255)
            for x in range(step, width, step):
                emphasize = x % (step * 5) == 0
                draw.line([(x, 0), (x, height)], fill=major, width=2 if emphasize else 1)
                draw.text((x + 2, 2), str(x), fill=major)
                draw.text((x + 2, height - 12), str(x), fill=major)
            for y in range(step, height, step):
                emphasize = y % (step * 5) == 0
                draw.line([(0, y), (width, y)], fill=major, width=2 if emphasize else 1)
                draw.text((2, y + 2), str(y), fill=major)
                draw.text((width - 34, y + 2), str(y), fill=major)
            base.save(grid_path, "PNG")
        return str(grid_path)
    except Exception:
        return ""


def screen_capture(monitor: int = 1) -> Dict[str, Any]:
    """Capture one monitor to a PNG artifact. Tries mss, then Pillow, then PowerShell .NET.

    Defaults to the PRIMARY monitor (mss index 1), not the stitched all-screens
    virtual box (index 0). On multi-monitor setups the stitched image put the
    click coordinate space out of sync with pyautogui's primary-monitor origin,
    so every model-chosen coordinate landed in the wrong place.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = SCREENSHOT_DIR / f"screen_{int(time.time() * 1000)}.png"

    try:
        import mss  # type: ignore

        with mss.mss() as grabber:
            monitors = grabber.monitors
            # monitors[0] is the all-screens VIRTUAL box (e.g. 8320x2012 across
            # several monitors); 1..n are physical monitors. Capturing index 0
            # made the model read coordinates off a stitched image that mapped
            # onto other monitors, so clicks missed entirely. Treat 0 or an
            # unspecified/invalid monitor as the PRIMARY (index 1) so image
            # coords == primary-monitor click coords. Only an explicit monitor
            # >= 1 selects a specific physical screen.
            default_index = 1 if len(monitors) > 1 else 0
            index = monitor if 1 <= monitor < len(monitors) else default_index
            region = monitors[index]
            shot = grabber.grab(region)
            mss.tools.to_png(shot.rgb, shot.size, output=str(target))
        width, height = shot.size[0], shot.size[1]
        return {
            "path": str(target),
            "width": width,
            "height": height,
            "backend": "mss",
            "monitor": index,
            "origin": {"left": region.get("left", 0), "top": region.get("top", 0)},
            "thumbnail_path": _write_thumbnail(target, width, height),
            "grid_path": _draw_coordinate_grid(target, width, height),
        }
    except Exception:
        pass

    try:
        from PIL import ImageGrab  # type: ignore

        image = ImageGrab.grab()  # primary monitor only
        image.save(target, "PNG")
        return {
            "path": str(target),
            "width": image.width,
            "height": image.height,
            "backend": "pillow",
            "monitor": monitor,
            "thumbnail_path": _write_thumbnail(target, image.width, image.height),
            "grid_path": _draw_coordinate_grid(target, image.width, image.height),
        }
    except Exception:
        pass

    if os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
            "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
            f"$bmp.Save('{target}', [System.Drawing.Imaging.ImageFormat]::Png); "
            "Write-Output ($b.Width.ToString() + 'x' + $b.Height.ToString())"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (result.stdout or "").strip()
        if result.returncode == 0 and target.exists():
            width, height = 0, 0
            if "x" in raw:
                try:
                    width, height = (int(part) for part in raw.split("x", 1))
                except ValueError:
                    pass
            return {"path": str(target), "width": width, "height": height, "backend": "powershell"}
        raise RuntimeError(f"PowerShell screen capture failed: {(result.stderr or raw)[:400]}")

    raise RuntimeError("No screenshot backend available. Install mss or Pillow: pip install mss Pillow")


def click(x: int, y: int, button: str = "left", double: bool = False) -> Dict[str, Any]:
    gui = _pyautogui()
    clean_button = button if button in {"left", "right", "middle"} else "left"
    if double:
        gui.doubleClick(x=x, y=y, button=clean_button)
    else:
        gui.click(x=x, y=y, button=clean_button)
    return {"x": x, "y": y, "button": clean_button, "double": double}


def move(x: int, y: int) -> Dict[str, Any]:
    gui = _pyautogui()
    gui.moveTo(x, y, duration=0.1)
    return {"x": x, "y": y}


def type_text(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Text is required.")
    gui = _pyautogui()
    gui.typewrite(text, interval=0.02)
    return {"typed_chars": len(text)}


def click_and_type(x: int, y: int, text: str, clear: bool = False, submit: bool = False) -> Dict[str, Any]:
    """Click a target, wait for focus, then type, as ONE deterministic action.

    Weak models fail the multi-turn motor sequence (move, click, verify focus,
    type) by dropping a step, most often typing before the field is focused.
    Bundling the whole sequence in the backend lets the model supply intent
    (where + what) instead of hand-motion choreography. clear selects and
    deletes existing text first; submit presses Enter after typing.
    """
    if not text:
        raise ValueError("Text is required.")
    gui = _pyautogui()
    gui.click(x=x, y=y)
    time.sleep(0.3)  # let the target window take keyboard focus before typing
    if clear:
        gui.hotkey("ctrl", "a")
        gui.press("delete")
    gui.typewrite(text, interval=0.02)
    if submit:
        gui.press("enter")
    return {"x": x, "y": y, "typed_chars": len(text), "cleared": clear, "submitted": submit}


# Friendly app names -> launch argv. Classic Win32 executables only, launched
# without a shell. A curated allowlist keeps launching deterministic and safe:
# the model asks for "notepad", not an arbitrary command line.
APP_LAUNCHERS: Dict[str, List[str]] = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "calc": ["calc.exe"],
    "paint": ["mspaint.exe"],
    "mspaint": ["mspaint.exe"],
    "explorer": ["explorer.exe"],
    "file explorer": ["explorer.exe"],
    "files": ["explorer.exe"],
    "control panel": ["control.exe"],
    "control": ["control.exe"],
    "cmd": ["cmd.exe"],
    "command prompt": ["cmd.exe"],
    "powershell": ["powershell.exe"],
    "terminal": ["powershell.exe"],
    "wordpad": ["write.exe"],
    "write": ["write.exe"],
    "task manager": ["taskmgr.exe"],
    "taskmgr": ["taskmgr.exe"],
    "snipping tool": ["snippingtool.exe"],
    "registry editor": ["regedit.exe"],
    "regedit": ["regedit.exe"],
}


def open_app(name: str) -> Dict[str, Any]:
    """Launch a known desktop app by friendly name, deterministically.

    Navigating the Start menu by pixel-clicking is the single least reliable
    GUI step for a weak model. For common apps we skip it entirely: the model
    says open_app("notepad") and the backend launches the real executable,
    then in-app work uses screen_capture + clicks. Unknown names are rejected
    with the supported list rather than guessed at.
    """
    key = " ".join((name or "").split()).lower()
    if not key:
        raise ValueError("An app name is required.")
    argv = APP_LAUNCHERS.get(key)
    if argv is None:
        supported = ", ".join(sorted({argv[0].replace('.exe', '') for argv in APP_LAUNCHERS.values()}))
        raise ValueError(f"Unknown app '{name}'. Supported apps: {supported}.")
    if os.name != "nt":
        raise RuntimeError("open_app currently supports Windows desktop apps only.")
    process = subprocess.Popen(argv)  # noqa: S603 - argv from a fixed allowlist, no shell
    time.sleep(0.6)  # give the window time to appear before the next screen_capture
    return {"app": key, "pid": process.pid, "command": " ".join(argv)}


def press_keys(keys: str) -> Dict[str, Any]:
    """Press a key or chord, e.g. 'enter', 'ctrl+s', 'alt+tab'."""
    if not keys.strip():
        raise ValueError("Keys are required.")
    gui = _pyautogui()
    parts = [part.strip().lower() for part in keys.split("+") if part.strip()]
    resolved = [KEY_ALIASES.get(part, part) for part in parts]
    if len(resolved) == 1:
        gui.press(resolved[0])
    else:
        gui.hotkey(*resolved)
    return {"keys": resolved}


def scroll(amount: int, x: int | None = None, y: int | None = None) -> Dict[str, Any]:
    gui = _pyautogui()
    if x is not None and y is not None:
        gui.moveTo(x, y, duration=0.05)
    gui.scroll(amount)
    return {"amount": amount}


def screen_size() -> Dict[str, Any]:
    try:
        gui = _pyautogui()
        width, height = gui.size()
        return {"width": int(width), "height": int(height)}
    except Exception:
        capture = screen_capture()
        return {"width": capture.get("width", 0), "height": capture.get("height", 0)}


def list_windows() -> List[Dict[str, Any]]:
    try:
        import pygetwindow  # type: ignore

        windows = []
        for window in pygetwindow.getAllWindows():
            title = (window.title or "").strip()
            if not title:
                continue
            windows.append({
                "title": title,
                "left": window.left,
                "top": window.top,
                "width": window.width,
                "height": window.height,
                "active": bool(window.isActive),
                "minimized": bool(window.isMinimized),
            })
        return windows
    except Exception:
        pass

    if os.name == "nt":
        script = (
            "Get-Process | Where-Object { $_.MainWindowTitle } | "
            "Select-Object -Property MainWindowTitle, Id | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        raw = (result.stdout or "").strip()
        if result.returncode == 0 and raw:
            import json

            data = json.loads(raw)
            rows = data if isinstance(data, list) else [data]
            return [{"title": row.get("MainWindowTitle"), "pid": row.get("Id")} for row in rows if row.get("MainWindowTitle")]
    return []


def focus_window(title: str) -> Dict[str, Any]:
    if not title.strip():
        raise ValueError("A window title (or part of it) is required.")
    try:
        import pygetwindow  # type: ignore

        matches = pygetwindow.getWindowsWithTitle(title)
        if matches:
            window = matches[0]
            if window.isMinimized:
                window.restore()
            window.activate()
            return {"focused": True, "title": window.title}
    except Exception:
        pass

    if os.name == "nt":
        safe = title.replace("'", "''")
        script = (
            "$shell = New-Object -ComObject WScript.Shell; "
            f"$ok = $shell.AppActivate('{safe}'); Write-Output $ok"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and "true" in (result.stdout or "").lower():
            return {"focused": True, "title": title}
    raise RuntimeError(f"No window matching '{title}' could be focused.")
