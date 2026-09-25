"""The one terminal-output surface for every CLI in this repo.

`rich` may be imported HERE AND NOWHERE ELSE. Every script that prints for a human
goes through these helpers, so the whole project looks like one program.

Two rendering modes, chosen per call by :func:`is_plain`:

* **rich mode** - stdout is a TTY: colour, a bar for progress, a rule under table headers.
* **plain mode** - stdout is a pipe/file/CI, or ``NO_COLOR`` / ``TERM=dumb`` / ``HHG_PLAIN=1``:
  fixed-width space-aligned ASCII. **Not one ANSI escape byte is written in plain mode** -
  the rich code path is never even reached, so ``make sheet | cat`` stays greppable.

Mode decision, in order:
  1. ``HHG_PLAIN`` set to anything but ``0`` -> plain (escape hatch for demos/CI).
  2. ``NO_COLOR`` set to a non-empty value -> plain.
  3. ``TERM`` is ``dumb`` or empty -> plain.
  4. ``FORCE_COLOR`` set to anything but ``0`` -> rich.
  5. otherwise: rich iff ``sys.stdout.isatty()``.

Nothing here has import-time side effects and `rich` is imported lazily inside
:func:`console`, so ``import ops.console`` costs a few hundred microseconds.

Every string this module prints is passed through :func:`redact` first, so a secret
that leaks into a path, an error string or a config dict never reaches the terminal.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Col",
    "Table",
    "console",
    "detail",
    "fail",
    "header",
    "is_plain",
    "joinlist",
    "money",
    "ok",
    "pct",
    "prob",
    "progress",
    "redact",
    "reset",
    "rule",
    "step",
    "summary",
    "truncate",
    "warn",
]

# Status tags. Fixed 4 columns + a space, so message text starts at column 5 in every
# script and `grep '^FAIL'` works on piped output.
_TAGS = {"step": "->", "ok": "OK", "warn": "WARN", "fail": "FAIL", "detail": ""}
_TAG_STYLES = {"step": "cyan", "ok": "green", "warn": "yellow", "fail": "bold red", "detail": "dim"}
_TAG_W = 4
_INDENT = " " * (_TAG_W + 1)
_GAP = "  "  # between table columns in plain mode
_MAX_RULE = 100
_ELLIPSIS = "..."  # ASCII on purpose: truncation marks survive `grep`/`cut`/CI logs

_console: Any = None
_secrets: tuple[str, ...] | None = None


# --------------------------------------------------------------------------- mode


def is_plain() -> bool:
    """True when output must be plain ASCII with no ANSI escapes. Re-evaluated per call."""
    hhg = os.environ.get("HHG_PLAIN")
    if hhg and hhg != "0":
        return True
    if os.environ.get("NO_COLOR"):
        return True
    if os.environ.get("TERM", "") in ("", "dumb"):
        return True
    force = os.environ.get("FORCE_COLOR")
    if force and force != "0":
        return False
    try:
        return not sys.stdout.isatty()
    except Exception:
        return True


def console() -> Any:
    """The cached `rich.console.Console` (created on first use, `rich` imported lazily).

    Writes to whatever `sys.stdout` is at write time, so pytest's capture works.
    Only reach for this when a helper below cannot express what you need; in plain
    mode the helpers bypass rich entirely and this Console is never touched.
    """
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console(soft_wrap=False, highlight=False, emoji=False)
    return _console


def reset() -> None:
    """Drop the cached Console and secret list. Tests call this after changing env vars."""
    global _console, _secrets
    _console = None
    _secrets = None


# ----------------------------------------------------------------------- redaction


def _secret_values() -> tuple[str, ...]:
    global _secrets
    if _secrets is None:
        marks = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "_KEY", "CREDENTIAL")
        vals = {v for k, v in os.environ.items() if any(m in k.upper() for m in marks) and isinstance(v, str) and len(v) >= 8}
        _secrets = tuple(sorted(vals, key=len, reverse=True))
    return _secrets


def redact(text: str) -> str:
    """Replace any secret-looking env value (TG_SECRET, *_API_KEY, *_TOKEN...) with ``***``.

    Values shorter than 8 characters are ignored so a stray ``DEBUG_KEY=1`` cannot shred
    unrelated output. Applied automatically to everything this module prints.
    """
    for v in _secret_values():
        if v in text:
            text = text.replace(v, "***")
    return text


def _out(text: str = "") -> None:
    print(redact(text))


def _rich(renderable: Any) -> None:
    console().print(renderable)


def _text(value: Any, style: str = "") -> Any:
    """A rich Text built from literal content. NEVER interpolate data into markup strings:
    cells like ``['device', 'history']`` contain square brackets and would blow up the
    markup parser. Text() takes the string literally.
    """
    from rich.text import Text

    return Text(redact("" if value is None else str(value)), style=style)


def _width() -> int:
    return min(shutil.get_terminal_size(fallback=(100, 24)).columns, _MAX_RULE)


def _wrap(text: str, width: int | None = None) -> list[str]:
    """Soft-wrap prose to the terminal width, preserving explicit line breaks.

    Only the plain path needs this: rich wraps its own renderables. Long unbroken
    tokens (paths, ids) are left intact rather than split mid-token.
    """
    limit = width or _width()
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
        else:
            lines.extend(textwrap.wrap(para, limit, break_long_words=False, break_on_hyphens=False) or [""])
    return lines


# ------------------------------------------------------------------- value helpers


def truncate(value: Any, width: int) -> str:
    """``str(value)`` cut to at most `width` characters, marked with ``...`` when cut."""
    s = "" if value is None else str(value)
    if width <= 0 or len(s) <= width:
        return s
    if width <= len(_ELLIPSIS):
        return s[:width]
    return s[: width - len(_ELLIPSIS)] + _ELLIPSIS


def joinlist(values: Iterable[Any] | None, *, empty: str = "-", sep: str = ",", max_items: int | None = None, sort: bool = False) -> str:
    """Format a list of short strings as a compact cell - never a Python list repr.

    ``joinlist(['device', 'history', 'memory'])`` -> ``'device,history,memory'``.
    Empty/None -> `empty`. With `max_items`, the overflow becomes ``'a,b +3'``.
    """
    items = [str(v) for v in (values or [])]
    if not items:
        return empty
    if sort:
        items = sorted(items)
    if max_items is not None and len(items) > max_items:
        return sep.join(items[:max_items]) + f" +{len(items) - max_items}"
    return sep.join(items)


def money(value: Any, *, symbol: str = "$", empty: str = "-") -> str:
    """USD with thousands separators: ``money(2140.94)`` -> ``'$2,140.94'``. None -> `empty`.

    Pass ``symbol=""`` when the column header already says USD.
    """
    if value is None or value == "":
        return empty
    try:
        return f"{symbol}{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def prob(value: Any, *, digits: int = 2, empty: str = "-") -> str:
    """A 0..1 probability as a fixed-width decimal: ``prob(0.07)`` -> ``'0.07'``."""
    if value is None or value == "":
        return empty
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value: Any, *, digits: int = 1, empty: str = "-") -> str:
    """A 0..1 probability as a percentage: ``pct(0.07)`` -> ``'7.0%'``."""
    if value is None or value == "":
        return empty
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------- status lines


def _status(kind: str, message: str) -> None:
    tag = _TAGS[kind]
    if is_plain():
        _out(f"{tag:<{_TAG_W}} {message}" if tag else f"{_INDENT}{message}")
        return
    from rich.text import Text

    line = Text()
    if tag:
        line.append(f"{tag:<{_TAG_W}} ", style=_TAG_STYLES[kind])
        line.append_text(_text(message))
    else:
        line.append_text(_text(_INDENT + message, style="dim"))
    _rich(line)


def step(message: str) -> None:
    """``->   about to do a thing`` - announce work before it happens."""
    _status("step", message)


def ok(message: str) -> None:
    """``OK   it worked``."""
    _status("ok", message)


def warn(message: str) -> None:
    """``WARN something is off but we continue``."""
    _status("warn", message)


def fail(message: str) -> None:
    """``FAIL something broke``. Does not exit or raise - the caller owns the exit code."""
    _status("fail", message)


def detail(message: str) -> None:
    """An indented continuation line under the previous status line."""
    _status("detail", message)


def rule(title: str = "") -> None:
    """A horizontal separator, optionally labelled - use it between phases of a run."""
    if is_plain():
        w = _width()
        _out(f"-- {redact(title)} ".ljust(w, "-") if title else "-" * w)
    else:
        from rich.rule import Rule

        _rich(Rule(_text(title), style="dim", align="left") if title else Rule(style="dim"))


# ------------------------------------------------------------------ header/summary


def _kv_lines(items: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> list[tuple[str, str]]:
    if not items:
        return []
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    return [(str(k), redact("" if v is None else str(v))) for k, v in pairs]


def header(name: str, about: str = "", config: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None) -> None:
    """Announce a run: what is running, what it will do, and the config it runs under.

    Call this once, first thing in `main()`, so a judge watching a demo video knows what
    they are looking at. `config` is the handful of settings that change the outcome
    (backend, db path, case count, output dir) - never secrets; they are redacted anyway.
    """
    kv = _kv_lines(config)
    pad = max((len(k) for k, _ in kv), default=0)
    if is_plain():
        w = _width()
        _out()
        _out(f"== {redact(name)} ".ljust(w, "="))
        if about:
            _out(f"{_INDENT}{redact(about)}")
        for k, v in kv:
            _out(f"{_INDENT}{k:<{pad}} : {v}")
        _out()
        return
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    if about:
        body.append(redact(about), style="italic")
    for i, (k, v) in enumerate(kv):
        if body.plain or i:
            body.append("\n")
        body.append(f"{k:<{pad}} ", style="dim")
        body.append(v, style="bold")
    console().print()
    _rich(Panel(body, title=_text(name, "bold cyan"), title_align="left", border_style="cyan", padding=(0, 1)))


def summary(title: str, items: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None, *, status: str = "ok") -> None:
    """End-of-run totals as an aligned key/value block. `status` is ok | warn | fail."""
    kv = _kv_lines(items)
    pad = max((len(k) for k, _ in kv), default=0)
    if is_plain():
        w = _width()
        _out()
        _out(f"== {redact(title)} ".ljust(w, "="))
        for k, v in kv:
            _out(f"{_INDENT}{k:<{pad}} : {v}")
        _out()
        return
    from rich.panel import Panel
    from rich.text import Text

    colour = {"ok": "green", "warn": "yellow", "fail": "red"}.get(status, "green")
    body = Text()
    for i, (k, v) in enumerate(kv):
        if i:
            body.append("\n")
        body.append(f"{k:<{pad}} ", style="dim")
        body.append(v, style="bold")
    console().print()
    _rich(Panel(body, title=_text(title, f"bold {colour}"), title_align="left", border_style=colour, padding=(0, 1)))


# ------------------------------------------------------------------------- tables


@dataclass(slots=True)
class Col:
    """One table column.

    header     text in the header row.
    align      'left' | 'right' | 'center'.
    width      exact width: header and cells are padded, longer ones truncated with '...'.
    max_width  cap on the DATA: the column is as wide as its widest cell up to this,
               but never narrower than its header, so headers stay readable.
    style      rich style for the cells (TTY only; ignored in plain mode).
    """

    header: str
    align: str = "left"
    width: int | None = None
    max_width: int | None = None
    style: str | None = None


class Table:
    """Aligned columnar output. Build it row by row, then call :meth:`print` once.

        t = Table(Col("case", width=8), Col("exposure", align="right"), title="Cases")
        t.add_row("HHG-001", money(166.97))
        t.print()

    Columns may be given as `Col` or as plain header strings. Cells are stringified and
    truncated per column, so nothing ever wraps mid-token. Zero rows prints the header
    plus ``(no rows)``. In plain mode the output is fixed-width, space-aligned, ASCII.
    """

    def __init__(self, *columns: Col | str, title: str = "", caption: str = "") -> None:
        self.columns: list[Col] = [c if isinstance(c, Col) else Col(str(c)) for c in columns]
        self.title = title
        self.caption = caption
        self.rows: list[tuple[list[str], str | None]] = []

    def add_row(self, *cells: Any, style: str | None = None) -> None:
        """Append a row. Missing trailing cells become empty; `style` tints the whole row on a TTY."""
        vals = ["" if c is None else str(c) for c in cells]
        vals += [""] * (len(self.columns) - len(vals))
        self.rows.append((vals[: len(self.columns)], style))

    def __len__(self) -> int:
        return len(self.rows)

    def _layout(self) -> tuple[list[str], list[list[str]], list[int]]:
        heads, widths = [], []
        for i, c in enumerate(self.columns):
            cells = [r[0][i] for r in self.rows]
            if c.width is not None:
                w = c.width  # exact: header and cells are both padded/truncated to this
            else:
                widest = max([0, *(len(x) for x in cells)])
                if c.max_width is not None:
                    widest = min(widest, c.max_width)
                # max_width caps the DATA; a column never shrinks below its own header,
                # so a cap never leaves you reading a mangled header like "verdict...".
                w = max(len(c.header), widest)
            widths.append(max(w, 1))
            heads.append(truncate(c.header, widths[i]))
        body = [[truncate(r[0][i], widths[i]) for i in range(len(self.columns))] for r in self.rows]
        return heads, body, widths

    def _pad(self, text: str, width: int, align: str) -> str:
        if align == "right":
            return text.rjust(width)
        if align == "center":
            return text.center(width)
        return text.ljust(width)

    def print(self) -> None:
        """Render the table to stdout."""
        if not self.columns:
            return
        heads, body, widths = self._layout()
        if is_plain():
            self._print_plain(heads, body, widths)
        else:
            self._print_rich(heads, body, widths)

    def _print_plain(self, heads: list[str], body: list[list[str]], widths: list[int]) -> None:
        if self.title:
            _out(redact(self.title))
        _out(_GAP.join(self._pad(h, widths[i], self.columns[i].align) for i, h in enumerate(heads)).rstrip())
        _out(_GAP.join("-" * w for w in widths))
        for cells in body:
            _out(_GAP.join(self._pad(c, widths[i], self.columns[i].align) for i, c in enumerate(cells)).rstrip())
        if not body:
            _out("(no rows)")
        if self.caption:
            for line in _wrap(redact(self.caption)):
                _out(line)

    def _print_rich(self, heads: list[str], body: list[list[str]], widths: list[int]) -> None:
        from rich import box
        from rich.table import Table as RichTable

        # Hand rich the widths we already computed and pin them. Left to itself rich
        # re-lays-out to the terminal and squeezes every column into ellipses ("HHG-0...",
        # "0.8...") even when the data would have fitted. pad_edge=False + padding (0,1)
        # makes the total exactly match the plain-mode grid: sum(widths) + 2*(ncols-1).
        t = RichTable(box=box.SIMPLE_HEAD, header_style="bold cyan", show_edge=False, pad_edge=False, expand=False, padding=(0, 1))
        for c, h, w in zip(self.columns, heads, widths):
            t.add_column(_text(h), justify=c.align, style=c.style, no_wrap=True, overflow="ellipsis", width=w)
        for cells, style in zip(body, [r[1] for r in self.rows]):
            t.add_row(*[_text(c) for c in cells], style=style)
        if self.title:
            _rich(_text(self.title, "bold"))
        _rich(t)
        if not body:
            _rich(_text("(no rows)", "dim"))
        if self.caption:
            _rich(_text(self.caption, "dim"))


# ----------------------------------------------------------------------- progress


class _Tick:
    """Handle yielded by :func:`progress`."""

    def __init__(self, label: str, total: int | None, every: float, task: Any = None, bar: Any = None) -> None:
        self.label, self.total, self.every = label, total, every
        self.n = 0
        self._task, self._bar = task, bar
        self._t0 = time.monotonic()
        self._last = self._t0

    def advance(self, n: int = 1) -> None:
        """Count `n` more items done, printing/redrawing at most every `every` seconds."""
        self.n += n
        if self._bar is not None:
            self._bar.update(self._task, advance=n)
            return
        now = time.monotonic()
        if now - self._last >= self.every:
            self._last = now
            self._line(now - self._t0)

    def _line(self, elapsed: float) -> None:
        of = f"{self.n}/{self.total}" if self.total else str(self.n)
        rate = f" {self.n / elapsed:,.0f}/s" if elapsed > 0.5 and self.n else ""
        _out(f"{_INDENT}{redact(self.label)} {of}{rate} {elapsed:.1f}s")


@contextmanager
def progress(label: str, total: int | None = None, *, every: float = 5.0) -> Iterator[_Tick]:
    """Progress for a long loop. A bar on a TTY; periodic ``n/N elapsed`` lines when piped.

        with progress("embedding rows", total=len(rows)) as p:
            for r in rows:
                embed(r)
                p.advance()

    `total=None` is fine (unknown length). On exit an ``OK`` line reports the final count
    and wall time. `every` is the minimum seconds between plain-mode lines.
    """
    def _done(t: _Tick) -> None:
        ok(f"{label} {t.n}{f'/{t.total}' if t.total else ''} in {time.monotonic() - t._t0:.1f}s")

    if is_plain():
        tick = _Tick(label, total, every)
        try:
            yield tick
        finally:
            _done(tick)
        return

    from rich.markup import escape
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

    cols: list[Any] = [SpinnerColumn(), TextColumn("[cyan]{task.description}")]
    if total:
        cols += [BarColumn(bar_width=30), TaskProgressColumn(), MofNCompleteColumn()]
    else:
        cols += [MofNCompleteColumn()]
    cols += [TimeElapsedColumn()]
    bar = Progress(*cols, console=console(), transient=True)
    tick = _Tick(label, total, every)
    try:
        # The summary line must be printed AFTER the Live display has stopped, or it gets
        # interleaved into the bar's redraw frames ("...0/20 0:00:00OK   scoring cases").
        with bar:
            tick = _Tick(label, total, every, bar.add_task(escape(redact(label)), total=total), bar)
            yield tick
    finally:
        _done(tick)
