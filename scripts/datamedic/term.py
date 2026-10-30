"""Terminal output: rich tables and colour on an interactive terminal, plain text everywhere else.

Plain text is used when rich is not installed, when output is piped or captured (agents, CI, tests),
when NO_COLOR is set, or when DATAMEDIC_PLAIN=1. The words are the same either way; colour only marks
status (green passed, amber warning, red failed).
"""

import os
import sys

from .common import optional


def _fancy():
    if os.environ.get("DATAMEDIC_PLAIN") or os.environ.get("NO_COLOR"):
        return None
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return None
    rich = optional("rich")
    if rich is None:
        return None
    from rich.console import Console
    return Console(highlight=False)


_CON = None
_CHECKED = False


def console():
    global _CON, _CHECKED
    if not _CHECKED:
        _CON, _CHECKED = _fancy(), True
    return _CON


STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan", "dim": "dim"}


def line(text, kind=None):
    con = console()
    if con is None:
        print(text)
    else:
        from rich.markup import escape
        con.print(f"[{STYLE[kind]}]{escape(text)}[/]" if kind else escape(text))


def heading(text):
    con = console()
    if con is None:
        print(text)
    else:
        from rich.markup import escape
        con.rule(f"[bold]{escape(text)}[/]", align="left")


def table(headers, rows, title=None, kinds=None):
    """`kinds`, if given, is one status kind per row (ok, warn, fail) used to colour that row."""
    con = console()
    if con is None:
        if title:
            print(title)
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
        widths = [min(w, 60) for w in widths]
        print("  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
        for r in rows:
            print("  " + "  ".join(str(c)[:60].ljust(w) for c, w in zip(r, widths)))
        return
    from rich.markup import escape
    from rich.table import Table
    t = Table(title=title, title_justify="left", header_style="bold", show_edge=False, pad_edge=False)
    for h in headers:
        t.add_column(str(h), overflow="fold")
    for i, r in enumerate(rows):
        k = kinds[i] if kinds else None
        t.add_row(*[escape(str(c)) for c in r], style=STYLE.get(k) if k in ("warn", "fail") else None)
    con.print(t)
