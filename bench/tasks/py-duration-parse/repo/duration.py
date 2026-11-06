"""Turn strings like 1h30m or 45s into seconds."""
import re

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    match = re.match(r"(\d+)([smhd])", text.strip())
    if not match:
        raise ValueError(f"bad duration: {text!r}")
    return int(match.group(1)) * UNITS[match.group(2)]


def format_duration(seconds):
    """The reverse: 5400 -> 1h30m."""
    parts = []
    for unit in ("d", "h", "m", "s"):
        amount = seconds // UNITS[unit]
        if amount:
            parts.append(f"{amount}{unit}")
            seconds -= amount
    return "".join(parts) or "0s"
