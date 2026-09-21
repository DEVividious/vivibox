"""What a person sees: helpers for tests that assert on the rendered view, not on widgets."""

import io

from rich.console import Console


def screen_text(app) -> str:
    """The current screen as the text a terminal would show, dialogs on top included."""
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    update = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)
    console.print(update)
    return "\n".join(line.rstrip() for line in console.export_text(styles=False).splitlines())
