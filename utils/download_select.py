"""
Textual-based interactive file selector for download.py --select.

Presents all filtered files in a selectable list with file names on the left
and sizes on the right.  A header shows selected/total counts and sizes.
Selecting (highlighting) an entry shows its relative directory path in a footer.
"""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Label, SelectionList
from textual.widgets.selection_list import Selection

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stats import fmt_size


class FileSelector(App[list[int] | None]):
    """Interactive file-selection TUI.

    Returns a list of indices (into the original items list) the user
    selected, or None if they quit without confirming.
    """

    TITLE = "Myrient Download Selector"

    CSS = """
    #header-bar {
        dock: top;
        height: 3;
        padding: 0 2;
        background: $primary-background;
        color: $text;
        content-align: center middle;
    }

    #path-bar {
        dock: bottom;
        height: 3;
        padding: 0 2;
        background: $primary-background;
        color: $text-muted;
    }

    SelectionList {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("a", "toggle_all", "Toggle all"),
        Binding("enter", "confirm", "Confirm"),
        Binding("escape", "quit_app", "Quit"),
    ]

    def __init__(
        self,
        items: list[tuple[str, Path, int]],
        root: Path,
    ) -> None:
        """items: list of (url, local_path, size_bytes) from collect_downloads."""
        super().__init__()
        self._items = items
        self._root = root
        self._total_size = sum(sz for _, _, sz in items)
        self._result: list[int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(self._summary_text(0, 0), id="header-bar")
        selections: list[Selection[int]] = []
        for idx, (_url, path, size) in enumerate(self._items):
            name = path.name
            size_str = fmt_size(size)
            label = f"{name}  [dim]{size_str}[/]"
            selections.append(Selection(label, idx, False))
        yield SelectionList[int](*selections)
        yield Label("", id="path-bar")
        yield Footer()

    def _summary_text(self, sel_count: int, sel_size: int) -> str:
        total_count = len(self._items)
        return (
            f"Selected: [bold]{sel_count:,}[/] / {total_count:,} files    "
            f"Size: [bold]{fmt_size(sel_size)}[/] / {fmt_size(self._total_size)}"
        )

    def _refresh_header(self) -> None:
        sel_list = self.query_one(SelectionList)
        selected_indices = set(sel_list.selected)
        sel_size = sum(
            self._items[idx][2] for idx in selected_indices
        )
        header = self.query_one("#header-bar", Label)
        header.update(self._summary_text(len(selected_indices), sel_size))

    @on(SelectionList.SelectionToggled)
    def _on_toggle(self, event: SelectionList.SelectionToggled) -> None:
        self._refresh_header()

    @on(SelectionList.SelectionHighlighted)
    def _on_highlight(self, event: SelectionList.SelectionHighlighted) -> None:
        if event.selection_index is not None and event.selection_index < len(self._items):
            _, path, _ = self._items[event.selection_index]
            try:
                rel = path.relative_to(self._root)
            except ValueError:
                rel = path
            self.query_one("#path-bar", Label).update(f"Path: {rel}")

    def action_toggle_all(self) -> None:
        sel_list = self.query_one(SelectionList)
        if sel_list.selected:
            sel_list.deselect_all()
        else:
            sel_list.select_all()
        self._refresh_header()

    def action_confirm(self) -> None:
        sel_list = self.query_one(SelectionList)
        self._result = list(sel_list.selected)
        self.exit(self._result)

    def action_quit_app(self) -> None:
        self.exit(None)


def run_selector(
    items: list[tuple[str, Path, int]],
    root: Path,
) -> list[tuple[str, Path, int]] | None:
    """Launch the TUI and return the selected items, or None if cancelled."""
    if not items:
        return []
    app = FileSelector(items, root)
    result = app.run()
    if result is None:
        return None
    return [items[i] for i in sorted(result)]
