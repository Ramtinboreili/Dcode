"""Single source of truth for Dcode's look.

The web UI gets both palettes as CSS custom properties (served as /theme.css);
the terminal uses the brand colors below, which read well on light *and* dark
terminals (body text uses the terminal's own foreground).
"""

from __future__ import annotations

# --- Web palettes ------------------------------------------------------------------
LIGHT: dict[str, str] = {
    "bg": "#FFFFFF",
    "sidebar": "#F7F9FC",
    "surface": "#FFFFFF",
    "surface-2": "#F1F4F9",       # hover rows, chips
    "surface-3": "#E6EBF3",       # pressed / selected
    "border": "#E4E9F0",
    "border-strong": "#CED6E2",
    "text": "#0F172A",
    "text-2": "#334155",
    "muted": "#64748B",
    "primary": "#2563EB",
    "primary-hover": "#1D4ED8",
    "primary-soft": "#EEF3FF",    # tinted backgrounds
    "primary-text": "#1D4ED8",
    "accent": "#0891B2",
    "user-bubble": "#EEF3FF",
    "code-bg": "#F6F8FB",
    "success": "#16A34A",
    "warning": "#B45309",
    "warning-soft": "#FFF7E8",
    "danger": "#DC2626",
    "danger-soft": "#FEF1F1",
    "shadow": "0 1px 2px rgba(15,23,42,.05), 0 8px 24px -8px rgba(15,23,42,.12)",
    "shadow-lg": "0 24px 64px -16px rgba(15,23,42,.28)",
}

DARK: dict[str, str] = {
    "bg": "#0F1218",
    "sidebar": "#141821",
    "surface": "#171C26",
    "surface-2": "#1D2330",
    "surface-3": "#262E3E",
    "border": "#252C3A",
    "border-strong": "#343E52",
    "text": "#E8ECF4",
    "text-2": "#C2CAD8",
    "muted": "#8B95A8",
    "primary": "#4C8DFF",
    "primary-hover": "#6AA1FF",
    "primary-soft": "#17233D",
    "primary-text": "#8DB6FF",
    "accent": "#38BDF8",
    "user-bubble": "#1C2A47",
    "code-bg": "#0B0E14",
    "success": "#34D399",
    "warning": "#FBBF24",
    "warning-soft": "#2A2110",
    "danger": "#F87171",
    "danger-soft": "#2C1517",
    "shadow": "0 1px 2px rgba(0,0,0,.3), 0 8px 24px -8px rgba(0,0,0,.5)",
    "shadow-lg": "0 24px 64px -16px rgba(0,0,0,.7)",
}

# Kept for callers that want "the" palette (the default theme is light).
TOKENS = LIGHT

# --- Terminal colors (work on light and dark backgrounds) ------------------------
PRIMARY = "#3B82F6"
PRIMARY_SOFT = "#60A5FA"
ACCENT = "#06B6D4"
MUTED = "#8B95A7"
TEXT = "default"          # terminal's own foreground → readable everywhere
SUCCESS = "#22C55E"
WARNING = "#F59E0B"
DANGER = "#EF4444"
SURFACE_2 = "#334155"     # completion-menu background (paired with white text)
PLAN = "#8B5CF6"          # plan mode (violet)

# The Dcode mark: a chamfered frame around a "D" and a prompt cursor.
LOGO_LINES = [
    "╱───────╲",
    "│  D ❯_ │",
    "╲───────╱",
]
LOGO = "\n".join(LOGO_LINES)


def _block(tokens: dict[str, str]) -> str:
    return "\n".join(f"  --{name}: {value};" for name, value in tokens.items())


def css_variables() -> str:
    """Both palettes as CSS variables; ``data-theme`` on <html> forces one."""
    light, dark = _block(LIGHT), _block(DARK)
    return (
        f":root {{\n{light}\n  color-scheme: light;\n}}\n"
        "@media (prefers-color-scheme: dark) {\n"
        f"  :root:not([data-theme=\"light\"]) {{\n{dark}\n  color-scheme: dark;\n  }}\n"
        "}\n"
        f":root[data-theme=\"dark\"] {{\n{dark}\n  color-scheme: dark;\n}}\n"
    )
