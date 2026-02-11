#!/usr/bin/env python3
"""
Unified Wallpaper Service (Hot-Swap Daemon)

A persistent daemon that manages desktop wallpapers with seamless transitions.
Supports video (mpvpaper), static images, and solid colors (swaybg).

Hot-swap mechanism:
1. SIGHUP triggers config reload
2. New wallpaper starts FIRST
3. Old wallpaper killed AFTER new is running
4. Result: seamless transition with no visible gap

Settings Hub controls wallpapers by:
1. Modifying the settings-hub wallpaper config (XDG_CONFIG_HOME/settings-hub/wallpaper.json)
2. Sending SIGHUP: systemctl --user kill -s HUP wallpaper.service

Usage:
    wallpaper-service.py [--once]

    --once: Set wallpaper and exit (for testing)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".config" / "settings-hub" / "wallpaper.json"

log = logging.getLogger("wallpaper-service")


class WallpaperType(Enum):
    VIDEO = "video"
    IMAGE = "image"
    SOLID = "solid"


@dataclass(frozen=True)
class WallpaperConfig:
    """Immutable wallpaper configuration."""
    active_type: WallpaperType
    video_path: Optional[Path]
    image_path: Optional[Path]
    solid_color: str

    @classmethod
    def load(cls) -> WallpaperConfig:
        """Load configuration from disk."""
        # Defaults
        active_type = WallpaperType.VIDEO
        video_path = None
        image_path = None
        solid_color = "#1a1a2e"

        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)

                if "active_type" in data:
                    try:
                        active_type = WallpaperType(data["active_type"])
                    except ValueError:
                        log.warning(f"Invalid active_type: {data['active_type']}")

                if "video" in data and data["video"].get("path"):
                    video_path = Path(data["video"]["path"])

                if "image" in data and data["image"].get("path"):
                    image_path = Path(data["image"]["path"])

                if "solid" in data and data["solid"].get("color"):
                    solid_color = data["solid"]["color"]

            except Exception as e:
                log.warning(f"Failed to load config: {e}")

        return cls(
            active_type=active_type,
            video_path=video_path,
            image_path=image_path,
            solid_color=solid_color,
        )

    @property
    def identity(self) -> str:
        """Unique identifier for change detection."""
        if self.active_type == WallpaperType.VIDEO:
            return f"video:{self.video_path}"
        elif self.active_type == WallpaperType.IMAGE:
            return f"image:{self.image_path}"
        else:
            return f"solid:{self.solid_color}"


# ─────────────────────────────────────────────────────────────────────────────
# Wallpaper Process Abstractions
# ─────────────────────────────────────────────────────────────────────────────

class WallpaperProcess(ABC):
    """Base class for wallpaper rendering processes."""

    def __init__(self, output: str):
        self.output = output
        self.process: Optional[subprocess.Popen] = None

    @abstractmethod
    def _build_command(self) -> list[str]:
        """Build the command to execute."""
        pass

    def _get_env(self) -> dict:
        """Get environment for subprocess. Override if needed."""
        return os.environ.copy()

    def start(self) -> bool:
        """Start the wallpaper process. Returns True if started successfully."""
        cmd = self._build_command()
        log.info(f"Starting: {' '.join(cmd)}")

        try:
            self.process = subprocess.Popen(
                cmd,
                env=self._get_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return True
        except Exception as e:
            log.error(f"Failed to start process: {e}")
            return False

    def is_alive(self) -> bool:
        """Check if process is still running."""
        return self.process is not None and self.process.poll() is None

    def terminate(self, timeout: float = 2.0) -> None:
        """Gracefully terminate the process."""
        if self.process is None or self.process.poll() is not None:
            return

        pid = self.process.pid
        self.process.terminate()

        try:
            self.process.wait(timeout=timeout)
            log.debug(f"Process {pid} terminated gracefully")
        except subprocess.TimeoutExpired:
            log.warning(f"Process {pid} didn't terminate, killing...")
            self.process.kill()
            self.process.wait(timeout=1)

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None


class MpvPaperProcess(WallpaperProcess):
    """Video wallpaper using mpvpaper."""

    def __init__(self, output: str, video_path: Path):
        super().__init__(output)
        self.video_path = video_path

    def _build_command(self) -> list[str]:
        return [
            "mpvpaper",
            "-o", "no-audio loop --really-quiet",
            self.output,
            str(self.video_path),
        ]

    def _get_env(self) -> dict:
        # Clear LD_LIBRARY_PATH to avoid Wayfire's custom pixman
        env = super()._get_env()
        env.pop("LD_LIBRARY_PATH", None)
        return env


class SwaybgImageProcess(WallpaperProcess):
    """Static image wallpaper using swaybg."""

    def __init__(self, output: str, image_path: Path):
        super().__init__(output)
        self.image_path = image_path

    def _build_command(self) -> list[str]:
        return [
            "swaybg",
            "-o", self.output,
            "-i", str(self.image_path),
            "-m", "fill",
        ]


class SwaybgColorProcess(WallpaperProcess):
    """Solid color wallpaper using swaybg."""

    def __init__(self, output: str, color: str):
        super().__init__(output)
        self.color = color

    def _build_command(self) -> list[str]:
        return [
            "swaybg",
            "-o", self.output,
            "-c", self.color,
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Monitor Detection
# ─────────────────────────────────────────────────────────────────────────────

def get_primary_monitor() -> str:
    """Get the primary monitor from monitor-detect service.

    Reads from /run/user/<uid>/primary-monitor which is created by
    monitor-detect.service.
    """
    runtime_file = Path(f"/run/user/{os.getuid()}/primary-monitor")

    # Wait up to 5 seconds for monitor-detect to create the file
    for attempt in range(10):
        if runtime_file.exists():
            try:
                monitor = runtime_file.read_text().strip()
                if monitor:
                    return monitor
            except Exception as e:
                log.warning(f"Failed to read {runtime_file}: {e}")

        if attempt < 9:
            time.sleep(0.5)

    # Emergency fallback
    log.error(f"{runtime_file} not found - monitor-detect.service may have failed")
    log.info("Attempting fallback to wlr-randr...")

    try:
        result = subprocess.run(
            ["wlr-randr"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line and not line.startswith(' '):
                    return line.split()[0]
    except Exception:
        pass

    log.error("Could not determine primary monitor!")
    return "DP-1"  # Last resort


# ─────────────────────────────────────────────────────────────────────────────
# Wallpaper Daemon
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CrashTracker:
    """Tracks crash frequency for rate limiting restarts."""
    count: int = 0
    last_time: float = 0.0
    window: float = 5.0  # Reset count if no crash within this window
    max_before_backoff: int = 5

    def record_crash(self) -> float:
        """Record a crash, return backoff time (0 if no backoff needed)."""
        now = time.time()

        if now - self.last_time > self.window:
            self.count = 1
        else:
            self.count += 1

        self.last_time = now

        if self.count > self.max_before_backoff:
            return min(30.0, self.count * 2.0)
        return 0.0

    def reset_if_stable(self) -> None:
        """Reset crash count if process has been stable."""
        if time.time() - self.last_time > 30:
            self.count = 0


class WallpaperDaemon:
    """Main daemon managing wallpaper lifecycle."""

    def __init__(self):
        self.current_process: Optional[WallpaperProcess] = None
        self.current_config: Optional[WallpaperConfig] = None
        self.output: Optional[str] = None
        self.crash_tracker = CrashTracker()

        # Signal flags (written by signal handlers, read by main loop)
        self._reload_requested = False
        self._shutdown_requested = False
        self._child_exited = False

    def _create_process(self, config: WallpaperConfig, output: str) -> Optional[WallpaperProcess]:
        """Create appropriate wallpaper process based on config with fallback chain."""
        attempts = [
            (WallpaperType.VIDEO, config.video_path, lambda p: MpvPaperProcess(output, p)),
            (WallpaperType.IMAGE, config.image_path, lambda p: SwaybgImageProcess(output, p)),
            (WallpaperType.SOLID, None, lambda _: SwaybgColorProcess(output, config.solid_color)),
        ]

        # Find starting point in fallback chain
        start_idx = next(
            (i for i, (t, _, _) in enumerate(attempts) if t == config.active_type),
            0
        )

        # Try from configured type, falling through on failure
        for wp_type, path, factory in attempts[start_idx:]:
            if wp_type == WallpaperType.SOLID:
                return factory(None)

            if path and path.exists():
                return factory(path)
            elif path:
                log.warning(f"{wp_type.value} file not found: {path}")

        return None

    def hot_swap(self, force: bool = False) -> bool:
        """
        Hot-swap to new wallpaper configuration.

        Strategy: Start new wallpaper FIRST, then kill old one.
        This ensures seamless transition with no visible gap.

        Returns True if swap succeeded.
        """
        config = WallpaperConfig.load()

        # Skip if nothing changed (unless forced)
        if not force and self.current_config and config.identity == self.current_config.identity:
            log.debug(f"Config unchanged ({config.identity}), skipping swap")
            return True

        log.info(f"Hot-swapping: {self.current_config.identity if self.current_config else 'none'} -> {config.identity}")

        # Create and start new process
        new_process = self._create_process(config, self.output)
        if new_process is None:
            log.error("Failed to create wallpaper process")
            return False

        if not new_process.start():
            return False

        # Brief delay for new wallpaper to initialize and render
        time.sleep(0.3)

        # Verify new process is still running
        if not new_process.is_alive():
            log.error("New wallpaper process died immediately")
            return False

        # NOW kill the old wallpaper (new one is already visible)
        if self.current_process:
            log.info(f"Killing old wallpaper (pid={self.current_process.pid})")
            self.current_process.terminate(timeout=1.0)

        # Update state
        self.current_process = new_process
        self.current_config = config
        log.info(f"Wallpaper switched successfully (pid={new_process.pid})")
        return True

    def _setup_signals(self) -> None:
        """Install signal handlers."""

        def on_sighup(signum, frame):
            self._reload_requested = True

        def on_sigterm(signum, frame):
            self._shutdown_requested = True

        def on_sigchld(signum, frame):
            self._child_exited = True

        signal.signal(signal.SIGHUP, on_sighup)
        signal.signal(signal.SIGTERM, on_sigterm)
        signal.signal(signal.SIGINT, on_sigterm)
        signal.signal(signal.SIGCHLD, on_sigchld)

    def run(self, once: bool = False) -> int:
        """Main entry point. Returns exit code."""
        self.output = get_primary_monitor()

        config = WallpaperConfig.load()
        log.info(f"Starting: type={config.active_type.value}, output={self.output}")

        # Start initial wallpaper
        self.current_process = self._create_process(config, self.output)
        if self.current_process is None or not self.current_process.start():
            log.error("Failed to start initial wallpaper")
            return 1

        self.current_config = config

        if once:
            log.info("Running in --once mode, exiting")
            return 0

        self._setup_signals()
        log.info(f"Daemon running (pid={self.current_process.pid})")
        log.info(f"Send SIGHUP to reload: kill -HUP {os.getpid()}")

        # ─── Event-driven main loop ───────────────────────────────────────
        # Uses signal.pause() to sleep until a signal arrives.
        # This is far more efficient than polling with timeouts.
        # ──────────────────────────────────────────────────────────────────

        while not self._shutdown_requested:
            # Sleep until ANY signal arrives (SIGHUP, SIGCHLD, SIGTERM, etc.)
            signal.pause()

            # Handle reload request (SIGHUP)
            if self._reload_requested:
                self._reload_requested = False
                log.info("Processing reload request...")
                # Force=True because systemd sends SIGHUP to entire cgroup,
                # which kills swaybg/mpvpaper. We must restart even if
                # config is unchanged.
                self.hot_swap(force=True)
                # Clear child_exited - the SIGCHLD from the killed process
                # arrives during hot_swap and should be ignored.
                self._child_exited = False
                continue

            # Handle child process exit (SIGCHLD)
            if self._child_exited:
                self._child_exited = False

                # Check if it was our wallpaper process
                if self.current_process and not self.current_process.is_alive():
                    returncode = self.current_process.process.returncode
                    log.warning(f"Wallpaper process exited with code {returncode}")

                    # Rate-limited restart
                    backoff = self.crash_tracker.record_crash()
                    if backoff > 0:
                        log.warning(f"Too many crashes (attempt {self.crash_tracker.count}), waiting {backoff:.0f}s...")
                        time.sleep(backoff)
                    else:
                        time.sleep(1)

                    self.hot_swap(force=True)
                else:
                    # SIGCHLD for some other child process, ignore
                    self.crash_tracker.reset_if_stable()

        # Clean shutdown
        log.info("Shutting down...")
        if self.current_process:
            self.current_process.terminate(timeout=2.0)

        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Configure logging for systemd (no timestamps, it adds its own)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    once_mode = "--once" in sys.argv
    daemon = WallpaperDaemon()
    sys.exit(daemon.run(once=once_mode))


if __name__ == "__main__":
    main()
