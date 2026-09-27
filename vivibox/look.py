"""How the view looks: its colours, and the few ways it draws a status, a key and a heading.

Every screen takes its colours from here, by meaning, never by name: `WAITING`, not yellow. The
Textual theme is built from the same palette, so the stylesheet's `$warning` and a list cell's
`WAITING` are one colour. What each colour means is in docs/ux-guidelines.md, §10.
"""

from __future__ import annotations

from rich.markup import escape
from textual.content import Content
from textual.theme import Theme

from . import ui

# Neutral surfaces, one accent for what takes your keys, and a colour per meaning.
BACKGROUND = "#15171c"
SURFACE = "#1b1e25"  # a dialog
PANEL = "#23272f"  # a field's band
SELECTION = "#2b313d"  # the row the cursor is on: lighter, never the accent
FOREGROUND = "#d5d8de"
MUTED = "#7d8390"  # metadata, hints, help
FAINT = "#535966"  # placeholders, frames, what is out of play
ACCENT = "#7aa2f7"  # the focused control, the primary button, a key in a hint
WORKING = "#7dcfff"  # an agent or the verification at work
WAITING = "#e0af68"  # something waits for you
SUCCESS = "#9ece6a"  # done, passed
ERROR = "#f7768e"  # failed, refused, destroys

THEME = Theme(
    name="vivibox",
    primary=ACCENT,
    secondary=WORKING,
    accent=ACCENT,
    warning=WAITING,
    error=ERROR,
    success=SUCCESS,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    dark=True,
    variables={
        "text-muted": MUTED,
        "foreground-muted": MUTED,
        "text-disabled": FAINT,
        "border": FAINT,
        "border-blurred": FAINT,
        **(CUSTOM := {"selection": SELECTION, "faint": FAINT}),
        "block-cursor-background": SELECTION,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": SELECTION,
        "block-cursor-blurred-foreground": FOREGROUND,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": PANEL,
        "input-cursor-background": ACCENT,
        "input-cursor-foreground": BACKGROUND,
        "input-selection-background": f"{ACCENT} 30%",
        "footer-background": BACKGROUND,
        "footer-key-foreground": ACCENT,
        "footer-key-background": BACKGROUND,
        "footer-description-foreground": MUTED,
        "footer-description-background": BACKGROUND,
        "footer-item-background": BACKGROUND,
        "scrollbar": PANEL,
        "scrollbar-hover": FAINT,
        "scrollbar-active": MUTED,
        "scrollbar-background": BACKGROUND,
        "scrollbar-corner-color": BACKGROUND,
        # The details panel: its title and sections in the foreground, never the accent.
        **{f"markdown-h{n}-color": FOREGROUND for n in (1, 2, 3)},
        **{f"markdown-h{n}-color": MUTED for n in (4, 5, 6)},
        "button-foreground": FOREGROUND,
        "button-color-foreground": BACKGROUND,
        "button-focus-text-style": "bold",
    },
)

# What a task's status is drawn with: a mark and a colour per kind, so the list reads without
# colour too. The spinner stands in the mark of a task at work.
MARKS = {
    ui.DECISION: ("●", WAITING),
    ui.FAILED: ("✕", ERROR),
    ui.IDLE: ("○", WAITING),
    ui.AT_WORK: ("", WORKING),
    ui.PARKED: ("‖", MUTED),
    ui.FINISHED: ("✓", SUCCESS),
}
DELETED = ("–", FAINT)


def badge(view: ui.TaskView, spinner: str = "") -> str:
    """A status as the list shows it, in Rich markup: its mark, then its words."""
    mark, color = MARKS[view.rank]
    return f"[{color}]{spinner or mark or ' '} {escape(view.status)}[/]"


def finished_badge(deleted: bool) -> str:
    mark, color = DELETED if deleted else MARKS[ui.FINISHED]
    return f"[{color}]{mark} {'deleted' if deleted else 'done'}[/]"


def muted(text: str) -> str:
    return f"[{MUTED}]{escape(text)}[/]"


def faint(text: str) -> str:
    return f"[{FAINT}]{escape(text)}[/]"


def colored(text: str, color: str) -> str:
    return f"[{color}]{escape(text)}[/]"


# A key and what it does, the same wherever a key is named: the footer, a dialog's frame.
def hints(*pairs: tuple[str, str]) -> Content:
    """`hints(("enter", "open"), ("esc", "close"))`: the keys bold in the accent colour, what
    they do muted, two spaces between pairs."""
    parts: list = []
    for i, (key, what) in enumerate(pairs):
        if i:
            parts.append("  ")
        parts += [(key, f"bold {ACCENT}"), " ", (what, MUTED)]
    return Content.assemble(*parts)


# The keys every dialog names in its frame, by what closes it.
ESC_CLOSES = ("esc", "close")
ESC_CANCELS = ("esc", "cancel")
