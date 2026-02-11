# Wallpaper Service

Unified wallpaper daemon for Wayland with seamless hot-swap transitions.

## Features

- Video wallpapers (via mpvpaper)
- Static image wallpapers (via swaybg)
- Solid color backgrounds
- Seamless hot-swap transitions (no visible gap)
- SIGHUP-based config reload

## How It Works

The daemon uses a hot-swap mechanism:
1. New wallpaper starts FIRST
2. Old wallpaper killed AFTER new is running
3. Result: seamless transition

## Installation

```bash
# Install in editable mode
pip install -e .

# Enable systemd service
systemctl --user daemon-reload
systemctl --user enable --now wallpaper-service
```

## Usage

```bash
# Run as daemon
wallpaper-service

# Test mode (set wallpaper and exit)
wallpaper-service --once
```

## Configuration

Config file: `~/.config/settings-hub/wallpaper.json`

```json
{
  "type": "video",
  "path": "/path/to/video.mp4"
}
```

Reload config:
```bash
systemctl --user kill -s HUP wallpaper-service
```

## Requirements

- Python 3.10+
- swaybg (for static images)
- mpvpaper (for video wallpapers)

## License

MIT
