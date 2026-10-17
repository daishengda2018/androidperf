"""FPS and jank via `dumpsys gfxinfo <package>`.

Strategy:
- reset gfxinfo at session start and after every sample
- on each tick, parse the summary header of a fresh `dumpsys gfxinfo`
- derive fps from `Total frames rendered / (Uptime - Stats since)` — both read
  from the dumpsys output itself. Host-side timing races with dumpsys latency
  and under-counts the window, inflating fps into the tens of thousands.
"""

from __future__ import annotations

import contextlib
import re

from adbutils import AdbDevice

_PATTERNS = {
    "frames_total": re.compile(r"^[ \t]*Total frames rendered:[ \t]*(\d+)[ \t]*$", re.MULTILINE),
    "jank_frames": re.compile(r"^[ \t]*Janky frames:[ \t]*(\d+)(?:[ \t]*\(([\d.]+)%\))?[ \t]*$", re.MULTILINE),
    "p50_ms": re.compile(r"^[ \t]*50th percentile:[ \t]*(\d+)ms[ \t]*$", re.MULTILINE),
    "p90_ms": re.compile(r"^[ \t]*90th percentile:[ \t]*(\d+)ms[ \t]*$", re.MULTILINE),
    "p95_ms": re.compile(r"^[ \t]*95th percentile:[ \t]*(\d+)ms[ \t]*$", re.MULTILINE),
    "p99_ms": re.compile(r"^[ \t]*99th percentile:[ \t]*(\d+)ms[ \t]*$", re.MULTILINE),
}
_UPTIME_MS_RE = re.compile(r"Uptime:\s*(\d+)")
_STATS_SINCE_NS_RE = re.compile(r"Stats since:\s*(\d+)ns")

_PERCENTILE_KEYS = ("p50_ms", "p90_ms", "p95_ms", "p99_ms")


def parse_gfxinfo(output: str) -> dict[str, float]:
    """Extract the summary numbers. `jank_pct` is returned separately from count."""
    result: dict[str, float] = {}
    for key, pattern in _PATTERNS.items():
        match = pattern.search(output)
        if not match:
            continue
        if key == "jank_frames":
            result["jank_frames"] = float(match.group(1))
            with contextlib.suppress(ValueError, TypeError):
                result["jank_pct"] = float(match.group(2))
        else:
            with contextlib.suppress(ValueError):
                result[key] = float(match.group(1))

    up_match = _UPTIME_MS_RE.search(output)
    since_match = _STATS_SINCE_NS_RE.search(output)
    if up_match and since_match:
        try:
            uptime_s = int(up_match.group(1)) / 1_000
            stats_since_s = int(since_match.group(1)) / 1_000_000_000
            window = uptime_s - stats_since_s
            if window > 0:
                result["_window_s"] = window
        except ValueError:
            pass

    # When no frames were rendered in the window, dumpsys reports the max
    # histogram bucket (≈4950 ms) as every percentile. Drop those sentinels
    # rather than emitting nonsensical frame-time values.
    if result.get("frames_total", 0) == 0:
        for k in _PERCENTILE_KEYS:
            result.pop(k, None)

    return result


def reset(device: AdbDevice, package: str) -> None:
    if isinstance(getattr(device, "command", None), list):
        device.shell("dumpsys", "gfxinfo", package, "reset")
    else:
        device.shell(f"dumpsys gfxinfo {package} reset")


def sample(device: AdbDevice, *, package: str, **_: object) -> dict[str, float]:
    """Sample gfxinfo and reset for the next window. FPS is derived from the
    on-device window (Uptime - Stats since) so it matches the counter."""
    out = device.shell(f"dumpsys gfxinfo {package}")
    parsed = parse_gfxinfo(out)
    reset(device, package)
    frames = parsed.get("frames_total", 0.0)
    window = parsed.pop("_window_s", 0.0)
    if window > 0 and frames > 0:
        parsed["fps"] = frames / window
    else:
        parsed["fps"] = 0.0
    return parsed


def frame_values(text):
    """共享解析算法，输出Case既有字段；零帧不返回伪百分位。"""
    parsed = parse_gfxinfo(text)
    mapping = {"frames_total": "frames", "jank_frames": "janky_frames", "p50_ms": "p50_ms", "p95_ms": "p95_ms"}
    result = {target: int(parsed[source]) for source, target in mapping.items() if source in parsed}
    if result.get("frames") and "janky_frames" in result:
        result["jank_rate"] = result["janky_frames"] / result["frames"]
    return result


def read_window(device, package, *, framestats=False):
    """外部窗口只读，不触发reset；调用方明确控制每轮边界。"""
    args = ["dumpsys", "gfxinfo", package]
    if framestats:
        args.append("framestats")
    if isinstance(getattr(device, "command", None), list):
        return device.shell(*args)
    return device.shell(" ".join(args))
