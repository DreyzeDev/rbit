"""Signature dark/light terminal themes and curated palettes for rbit."""

from textual.theme import Theme

RBIT_DARK = Theme(
    name="rbit-dark",
    primary="#fab283",      # Build mode warm amber/peach accent
    secondary="#5c9cf5",    # Plan mode azure blue accent
    accent="#9d7cd8",       # Violet highlight
    warning="#f5a742",
    error="#e06c75",
    success="#7fd88f",
    foreground="#eeeeee",
    background="#0a0a0a",
    surface="#141414",
    panel="#1e1e1e",
    dark=True,
)

RBIT_LIGHT = Theme(
    name="rbit-light",
    primary="#d97706",
    secondary="#2563eb",
    accent="#7c3aed",
    warning="#d97706",
    error="#dc2626",
    success="#16a34a",
    foreground="#1a1a1a",
    background="#ffffff",
    surface="#f5f5f5",
    panel="#ebebeb",
    dark=False,
)

CURATED_THEMES: list[tuple[str, str]] = [
    ("rbit-dark", "rbit Dark (Default)"),
    ("rbit-light", "rbit Light"),
    ("catppuccin-mocha", "Catppuccin Mocha"),
    ("tokyo-night", "Tokyo Night"),
    ("gruvbox", "Gruvbox Dark"),
    ("dracula", "Dracula"),
    ("rose-pine", "Rosé Pine"),
    ("nord", "Nord"),
    ("flexoki", "Flexoki"),
    ("monokai", "Monokai"),
]
