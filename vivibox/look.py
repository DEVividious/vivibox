"""How the view looks: its colours, and the few ways it draws a status, a key and a heading.

Every screen takes its colours from here, by meaning, never by name: `WAITING`, not yellow. The
Textual theme is built from the same palette, so the stylesheet's `$warning` and a list cell's
`WAITING` are one colour. What each colour means is in docs/ux-guidelines.md, §10.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape
from textual.content import Content
from textual.theme import Theme

from . import ui

# Three dark layers, lighter as they come forward: the list, a dialog, a field in it. Text in four
# steps: what you read, what explains it, metadata, what is out of play. One accent for what takes
# your keys; a colour per meaning, never brighter than the text.
BACKGROUND = "#15181e"  # the list
SURFACE = "#1d2129"  # a dialog, the details panel
PANEL = "#252a34"  # a field, an open list: what stands forward in a dialog
BORDER = "#3b4352"  # a dialog's frame, a rule, a text field's frame
SELECTION = "#29364a"  # the row the cursor is on: a tint, never the accent
FOREGROUND = "#e6eaf0"  # what you read: values, task names, goals, options
SECONDARY = "#aab2c0"  # what explains it: labels, headings, help, a summary, costs
MUTED = "#7a8496"  # metadata: times, hints, placeholders, empty states
FAINT = "#4e5767"  # a dot in an empty cell, the project in a task's id: there, not read
DISABLED = "#5d6574"  # a control that does nothing now; drawn dim as well, unlike metadata
ACCENT = "#79a9ff"  # the focused control, a key in a hint
WORKING = "#7cc7e8"  # an agent or the verification at work
WAITING = "#e2b563"  # something waits for you
SUCCESS = "#95cf6f"  # done, passed
ERROR = "#e17a85"  # failed, refused, destroys

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
        "text-disabled": DISABLED,
        "border": BORDER,
        "border-blurred": BORDER,
        **(
            CUSTOM := {
                "selection": SELECTION,
                "faint": FAINT,
                "frame": BORDER,
                "text-secondary": SECONDARY,
                "text-disabled-dim": DISABLED,
            }
        ),
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
        "scrollbar-hover": BORDER,
        "scrollbar-active": MUTED,
        "scrollbar-background": BACKGROUND,
        "scrollbar-corner-color": BACKGROUND,
        # The details panel: its title and sections in the foreground, never the accent.
        **{f"markdown-h{n}-color": FOREGROUND for n in (1, 2, 3)},
        **{f"markdown-h{n}-color": SECONDARY for n in (4, 5, 6)},
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
    ui.PARKED: ("‖", SECONDARY),
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


def secondary(text: str) -> str:
    return f"[{SECONDARY}]{escape(text)}[/]"


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


@dataclass(frozen=True)
class Explained:
    """What a field or an option is, for the help under a dialog's fields (widgets.ContextHelp):
    a title, a badge, a diagram and a sentence; facts as name and value; a last line of what it
    costs you; and all of it in a line or two for a short terminal."""

    title: str
    summary: str = ""
    badge: str = ""
    diagram: str = ""
    facts: tuple[tuple[str, str], ...] = ()
    tradeoff: str = ""
    compact: str = ""

    def lines(self, full: bool) -> Content:
        """Full: the title, its badge and diagram on one line, the summary, the facts, the
        trade-off. Short: the title and diagram, then the compact line. In weight, the title
        first, then the diagram and the summary, the facts, and the trade-off last."""
        head: list = [(self.title, f"bold {FOREGROUND}")]
        if self.diagram:
            head += ["   ", (self.diagram, f"bold {SECONDARY}")]
        if self.badge:
            head += ["   ", (self.badge, SUCCESS)]
        if not full:
            return Content.assemble(*head, "\n", (self.compact or self.summary, SECONDARY))
        parts: list = [*head]
        if self.summary:
            parts += ["\n", (self.summary, SECONDARY)]
        width = max((len(name) for name, _ in self.facts), default=0) + 2
        for name, value in self.facts:
            parts += ["\n", (name.ljust(width), MUTED), (value, FOREGROUND)]
        if self.tradeoff:
            parts += ["\n", (self.tradeoff, MUTED)]
        return Content.assemble(*parts)


def flow(name: str, models: dict[str, str], rounds: int) -> Explained:
    """An orchestration mode as the help shows it, under n's Flow and k's flow row: its diagram
    and what it is, and the models it would run on (models: a role's model, by role; a mode's
    reviewer without one is the writer's), roles one agent plays beside its model."""
    from .config import DEFAULT_ORCHESTRATION, ORCHESTRATION_MODES

    mode = ORCHESTRATION_MODES[name]
    shown = []
    # Which roles an agent plays is said by Sessions; here each agent and its model.
    for role, agent, _ in mode.agents:
        model = models.get(role) or f"{models.get('writer', '')} (the writer's)"
        shown.append(f"{agent} {model}")
    return Explained(
        mode.label,
        mode.summary,
        badge="recommended" if name == DEFAULT_ORCHESTRATION else "",
        diagram=mode.flow,
        facts=(
            ("Sessions", mode.sessions),
            ("Models", " · ".join(shown)),
            ("Review", mode.review),
            ("Rounds", f"up to {rounds} fix turns after {mode.rounds}"),
            ("Best for", mode.best_for),
        ),
        tradeoff=mode.tradeoff,
        compact=mode.compact,
    )


# The keys every dialog names in its frame, by what closes it.
ESC_CLOSES = ("esc", "close")
ESC_CANCELS = ("esc", "cancel")
