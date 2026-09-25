"""ops/console.py is the single terminal-output surface for every CLI in the repo.

The contract these tests defend:
  * piped / non-TTY output contains NO ANSI escape bytes, ever (make, CI and `| cat` rely on it);
  * NO_COLOR and TERM=dumb force that same plain path; FORCE_COLOR opts back in;
  * plain tables are fixed-width and space-aligned, with per-column truncation;
  * the list helper never emits a Python list repr;
  * a zero-row table does not crash.

No real terminal is involved: stdout is a StringIO and the mode is driven by env vars.
"""
from __future__ import annotations

import io
import re
import sys

import pytest

from ops import console as C

# CSI / OSC / any other escape sequence. If this matches piped output, we have a bug.
ANSI = re.compile(r"\x1b")


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch):
    """Default every test to the piped case: no TTY, no colour forcing, clean cache."""
    for var in ("NO_COLOR", "FORCE_COLOR", "HHG_PLAIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    C.reset()
    yield
    C.reset()


class _Cap:
    """Accumulating view over pytest's stdout capture (readouterr() drains, we must not)."""

    def __init__(self, capsys):
        self._capsys, self._buf = capsys, ""

    def getvalue(self) -> str:
        self._buf += self._capsys.readouterr().out
        return self._buf


@pytest.fixture
def cap(capsys):
    """Captured stdout. pytest's replacement reports isatty() False, i.e. the piped case."""
    assert not sys.stdout.isatty()
    return _Cap(capsys)


def _sample(t: C.Table) -> C.Table:
    t.add_row("HHG-017", "risk_score", C.prob(0.099, digits=3), C.joinlist([], empty="-"),
              C.joinlist(["device", "history", "memory"]), C.money(0.0), "closed_legitimate")
    t.add_row("HHG-008", "customer_report", C.prob(0.957, digits=3), C.joinlist(["customer", "memory"]),
              C.joinlist(["device", "history"]), C.money(166.97), "closed_fraud")
    return t


def _table() -> C.Table:
    return C.Table(
        C.Col("case", width=8),
        C.Col("trigger", max_width=12),
        C.Col("cms", align="right", width=6),
        C.Col("fraud"),
        C.Col("legit"),
        C.Col("exposure", align="right", width=10),
        C.Col("status", max_width=10),
        title="Engine cases",
    )


# ------------------------------------------------------------------ mode selection


def test_non_tty_is_plain(cap):
    assert C.is_plain() is True


def test_no_color_forces_plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")  # NO_COLOR must win
    assert C.is_plain() is True


def test_dumb_term_forces_plain(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert C.is_plain() is True


def test_hhg_plain_overrides_force_color(monkeypatch):
    monkeypatch.setenv("HHG_PLAIN", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert C.is_plain() is True
    monkeypatch.setenv("HHG_PLAIN", "0")  # "0" means "do not force plain"
    assert C.is_plain() is False


def test_force_color_opts_into_rich_without_a_tty(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert C.is_plain() is False


def test_tty_detection_uses_stdout_isatty(monkeypatch):
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", FakeTTY())
    assert C.is_plain() is False


# ------------------------------------------------------------- no escapes when piped


def test_no_ansi_anywhere_on_the_plain_path(cap):
    C.header("engine.run_cases", "deterministic drafts", {"backend": "duckdb", "cases": 20})
    C.step("loading facts")
    C.ok("20 cases scored")
    C.warn("1 case escalated")
    C.fail("validator error")
    C.detail("missing field: pattern")
    C.rule("phase 2")
    _sample(_table()).print()
    C.summary("done", {"cases": 20, "errors": 0})
    with C.progress("embedding", total=3, every=0.0) as p:
        for _ in range(3):
            p.advance()
    out = cap.getvalue()
    assert not ANSI.search(out), f"ANSI escape leaked into piped output: {out!r}"
    assert out.isprintable() or "\n" in out


def test_no_ansi_under_no_color(monkeypatch, cap):
    monkeypatch.setenv("NO_COLOR", "1")
    C.reset()
    C.header("x", "y", {"k": "v"})
    _sample(_table()).print()
    C.summary("done", {"n": 1})
    assert not ANSI.search(cap.getvalue())


def test_square_brackets_in_data_do_not_break_or_escape(cap):
    """A raw list repr in a message must survive verbatim: rich markup would choke on it."""
    C.ok("F=['device', 'history'] L=[]")
    t = C.Table("a")
    t.add_row("['device', 'history']")
    t.print()
    out = cap.getvalue()
    assert "F=['device', 'history'] L=[]" in out
    assert not ANSI.search(out)


# ----------------------------------------------------------------- status line shape


def test_status_tags_are_ascii_and_aligned(cap):
    C.step("a")
    C.ok("b")
    C.warn("c")
    C.fail("d")
    C.detail("e")
    lines = cap.getvalue().splitlines()
    assert lines == ["->   a", "OK   b", "WARN c", "FAIL d", "     e"]
    assert all(line.isascii() for line in lines)
    # every message starts in the same column -> greppable, scannable
    assert len({len(line) - len(line.lstrip(" ")) if line.startswith(" ") else 5 for line in lines}) == 1


# ------------------------------------------------------------------ table rendering


def test_plain_table_columns_line_up(cap):
    _sample(_table()).print()
    lines = cap.getvalue().splitlines()
    assert lines[0] == "Engine cases"
    header, sep, *rows = lines[1:]
    assert set(sep) == {"-", " "}
    assert "\x1b" not in sep and "|" not in sep and "+" not in sep
    # the separator row defines the grid; every data row must respect it
    starts = [m.start() for m in re.finditer(r"-+", sep)]
    assert starts[0] == 0
    for row in rows:
        for s in starts[1:]:
            if len(row) > s:
                # a column boundary never lands mid-token
                assert row[s - 1] == " ", f"column at {s} not aligned in {row!r}"


def test_plain_table_has_no_box_drawing_characters(cap):
    _sample(_table()).print()
    out = cap.getvalue()
    assert out.isascii()
    for ch in "│┃─━┌┐└┘├┤┬┴┼╭╮╰╯═║":
        assert ch not in out


def test_fixed_width_column_pads_and_truncates(cap):
    t = C.Table(C.Col("id", width=8), C.Col("note", width=10))
    t.add_row("HHG-1", "short")
    t.add_row("HHG-000001-XL", "a very long note indeed")
    t.print()
    lines = cap.getvalue().splitlines()  # no title: header, separator, then rows
    assert lines[0] == "id        note"  # 8-wide + 2-space gap
    assert lines[1] == "--------  ----------"
    assert lines[2].startswith("HHG-1     ")  # padded to 8 + gap
    assert lines[3].startswith("HHG-0...")  # truncated to exactly 8 with an ASCII marker
    assert lines[3].endswith("a very ...")  # truncated to exactly 10
    for line in lines[2:]:
        assert len(line.rstrip()) <= 8 + 2 + 10


def test_max_width_is_a_cap_not_a_floor(cap):
    t = C.Table(C.Col("x", max_width=20))
    t.add_row("abc")
    t.print()
    lines = cap.getvalue().splitlines()
    # natural width is the widest cell (3), not the 20-wide cap
    assert lines[0] == "x"  # header padded to 3 then rstripped
    assert lines[1] == "---"
    assert lines[2] == "abc"


def test_max_width_never_truncates_the_header(cap):
    """A cap on the data must not leave a judge reading 'verdict...' as a column name.

    The header is a floor on the column width, and cells are then cut to that final
    width - never to a cap narrower than the header, which would waste the space.
    """
    t = C.Table(C.Col("verdict_pre", max_width=4))
    t.add_row("legitimate")
    t.print()
    lines = cap.getvalue().splitlines()
    assert lines[0] == "verdict_pre"  # header intact, not "verd..."
    assert lines[1] == "-" * len("verdict_pre")
    assert lines[2].rstrip() == "legitimate"  # fits the header-floored width, so kept whole

    # a header shorter than the cap: now the cap really bites, at the final width
    t2 = C.Table(C.Col("verdict", max_width=4))
    t2.add_row("legitimate")
    t2.print()
    lines2 = cap.getvalue().splitlines()[3:]
    assert lines2[0] == "verdict"
    assert lines2[2].rstrip() == "legi..."  # cut to the 7-wide header floor, not to 4


def test_width_does_truncate_the_header(cap):
    """An exact width is the caller's explicit instruction and binds the header too."""
    t = C.Table(C.Col("verdict_pre", width=6))
    t.add_row("legitimate")
    t.print()
    assert cap.getvalue().splitlines()[0] == "ver..."


def test_right_alignment(cap):
    t = C.Table(C.Col("amount", align="right", width=10))
    t.add_row(C.money(166.97))
    t.add_row(C.money(2140.94))
    t.print()
    rows = cap.getvalue().splitlines()[2:]
    assert rows[0] == "   $166.97"
    assert rows[1] == " $2,140.94"


def test_empty_table_does_not_crash(cap):
    C.Table(C.Col("a"), C.Col("b"), title="Nothing").print()
    out = cap.getvalue()
    assert "(no rows)" in out
    assert not ANSI.search(out)


def test_table_with_no_columns_is_a_noop(cap):
    C.Table().print()
    assert cap.getvalue() == ""


def test_missing_trailing_cells_are_filled(cap):
    t = C.Table(C.Col("a", width=4), C.Col("b", width=4), C.Col("c", width=4))
    t.add_row("x")
    t.print()
    assert len(t) == 1
    assert cap.getvalue().splitlines()[2].rstrip() == "x"


def test_string_columns_are_accepted(cap):
    t = C.Table("one", "two")
    t.add_row(1, 2)
    t.print()
    assert cap.getvalue().splitlines()[0] == "one  two"


# -------------------------------------------------------------------- value helpers


def test_joinlist_is_never_a_python_repr():
    assert C.joinlist(["device", "history", "memory"]) == "device,history,memory"
    assert "[" not in C.joinlist(["a"]) and "'" not in C.joinlist(["a"])
    assert C.joinlist([]) == "-"
    assert C.joinlist(None) == "-"
    assert C.joinlist([], empty="none") == "none"
    assert C.joinlist(["b", "a"], sort=True) == "a,b"
    assert C.joinlist(["a", "b", "c", "d"], max_items=2) == "a,b +2"
    assert C.joinlist({"device", "history"}, sort=True) == "device,history"


def test_money_and_probability_formatting():
    assert C.money(2140.94) == "$2,140.94"
    assert C.money(0) == "$0.00"
    assert C.money(166.97, symbol="") == "166.97"
    assert C.money(None) == "-"
    assert C.money("n/a") == "n/a"
    assert C.prob(0.07) == "0.07"
    assert C.prob(0.099, digits=3) == "0.099"
    assert C.prob(None) == "-"
    assert C.pct(0.07) == "7.0%"
    assert C.pct(0.5, digits=0) == "50%"


def test_truncate():
    assert C.truncate("abcdef", 10) == "abcdef"
    assert C.truncate("abcdef", 6) == "abcdef"
    assert C.truncate("abcdef", 5) == "ab..."
    assert len(C.truncate("abcdef", 5)) == 5
    assert C.truncate("abcdef", 3) == "abc"
    assert C.truncate("abcdef", 0) == "abcdef"
    assert C.truncate(None, 4) == ""
    assert C.truncate(12345, 3) == "123"


# --------------------------------------------------------------------- redaction


def test_secrets_never_reach_the_terminal(monkeypatch, cap):
    monkeypatch.setenv("TG_SECRET", "s3cr3t-value-abcdefgh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789abcdef")
    C.reset()
    C.header("run", "go", {"secret": "s3cr3t-value-abcdefgh"})
    C.ok("connected with sk-ant-0123456789abcdef")
    out = cap.getvalue()
    assert "s3cr3t-value-abcdefgh" not in out
    assert "sk-ant-0123456789abcdef" not in out
    assert "***" in out


def test_short_env_values_are_not_redacted(monkeypatch):
    monkeypatch.setenv("DEBUG_KEY", "1")
    C.reset()
    assert C.redact("value is 1 today") == "value is 1 today"


# ---------------------------------------------------------------------- progress


def test_progress_plain_emits_periodic_lines_and_a_final_ok(cap):
    with C.progress("embedding rows", total=4, every=0.0) as p:
        for _ in range(4):
            p.advance()
        assert p.n == 4
    lines = cap.getvalue().splitlines()
    assert any("embedding rows 1/4" in line for line in lines)
    assert lines[-1].startswith("OK   embedding rows 4/4 in ")
    assert not ANSI.search(cap.getvalue())


def test_progress_without_a_total(cap):
    with C.progress("scanning", every=0.0) as p:
        p.advance(7)
    assert cap.getvalue().splitlines()[-1].startswith("OK   scanning 7 in ")


def test_progress_rate_limits_plain_lines(cap):
    with C.progress("quiet", total=100, every=3600.0) as p:
        for _ in range(100):
            p.advance()
    # only the closing OK line: nothing spammed in between
    assert len(cap.getvalue().splitlines()) == 1


def test_progress_reports_final_count_even_on_exception(cap):
    with pytest.raises(RuntimeError), C.progress("work", total=10, every=3600.0) as p:
        p.advance(3)
        raise RuntimeError("boom")
    assert cap.getvalue().splitlines()[-1].startswith("OK   work 3/10 in ")


# ------------------------------------------------------------------ rich mode works


def test_rich_mode_renders_and_does_emit_colour(monkeypatch):
    """The other half of the contract: on a TTY we actually do get styled output."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    C.reset()
    buf = io.StringIO()
    from rich.console import Console

    monkeypatch.setattr(C, "_console", Console(file=buf, force_terminal=True, width=100, highlight=False, emoji=False))
    C.header("engine.run_cases", "drafts", {"backend": "duckdb"})
    C.ok("done")
    _sample(_table()).print()
    C.summary("totals", {"cases": 2})
    out = buf.getvalue()
    assert ANSI.search(out), "expected colour on a forced TTY"
    assert "HHG-017" in out and "device,history,memory" in out
    assert "$166.97" in out


def test_rich_mode_survives_brackets_and_empty_tables(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    C.reset()
    buf = io.StringIO()
    from rich.console import Console

    monkeypatch.setattr(C, "_console", Console(file=buf, force_terminal=True, width=100, highlight=False, emoji=False))
    C.ok("F=['device'] /bad/markup[")
    C.rule("phase [1]")
    C.Table(C.Col("a"), title="Empty [x]").print()
    assert "(no rows)" in buf.getvalue()


def test_console_is_cached():
    assert C.console() is C.console()
    first = C.console()
    C.reset()
    assert C.console() is not first


def test_import_has_no_side_effects_and_does_not_import_rich_eagerly():
    import subprocess

    code = "import sys; import ops.console; print('rich' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False", "importing ops.console must not pull in rich"
    assert r.stderr == ""
