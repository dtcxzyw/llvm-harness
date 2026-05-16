"""Issue description types for the LLVM harness."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Reproducer:
  """Reproducer specification — file, commands, and test bodies."""

  file: str
  commands: list[str]
  tests: list[dict]  # [{test_name, test_body, additional_args?}]


@dataclass
class IssueCard:
  """Describes an LLVM issue.  Defaults align with open issues against trunk.

  Construct directly::

      IssueCard(
          bug_type="crash",
          reproducers=[Reproducer(
              file="test.ll",
              commands=["opt -O2 < %s"],
              tests=[{"test_name": "<module>", "test_body": "..."}],
          )],
      )
  """

  # Required
  bug_type: str  # "crash" | "miscompilation" | "hang"
  reproducers: list[Reproducer]

  # Optional (defaults for open issues)
  base_commit: str | None = None  # None → HEAD
  test_commit: str | None = None  # None → same as base_commit
  lit_test_dir: list[str] | None = None

  # Optional (context)
  issue: dict | None = None  # {title, body, author, labels, comments}


# ---------------------------------------------------------------------------
# Lit-style reproducer parsing
# ---------------------------------------------------------------------------

_RUN_RE = re.compile(r"^\s*;\s*RUN:\s*(.+?)\s*$")
_BUG_RE = re.compile(r"^\s*;\s*BUG:\s*(\S+)\s*$")

SUPPORTED_BUG_TYPES = {"crash", "miscompilation"}


def _sanitize_run_command(raw: str) -> str:
  """Strip lit wrappers (``not``, ``not --crash``, ``env VAR=val``) and any
  pipeline tail (``| FileCheck ...``) from a ``; RUN:`` line, leaving a bare
  ``opt …`` command suitable for ``verify_dispatch``.
  """
  cmd = raw.strip()

  # Drop pipeline tail — we don't run FileCheck in autofix mode.
  pipe = cmd.find("|")
  if pipe != -1:
    cmd = cmd[:pipe].strip()

  # Strip ``not`` / ``not --crash`` lit wrappers.
  tokens = cmd.split()
  while tokens and tokens[0] == "not":
    tokens.pop(0)
    if tokens and tokens[0] == "--crash":
      tokens.pop(0)

  # Strip leading ``env VAR=val VAR2=val2 …`` prefixes.
  while tokens and tokens[0] == "env":
    tokens.pop(0)
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
      tokens.pop(0)

  return " ".join(tokens)


def parse_lit_reproducer(path: str | Path) -> tuple[Reproducer, str]:
  """Parse an ad-hoc reproducer ``.ll`` file with embedded directives.

  The file must contain:

  * ``; BUG: crash`` or ``; BUG: miscompilation``
  * ``; RUN: opt …`` — the reproduction command (FileCheck pipelines stripped)

  Returns ``(Reproducer, bug_type)``. Raises ``ValueError`` on any failure.
  """
  p = Path(path).resolve()
  if not p.exists():
    raise ValueError(f"Reproducer file not found: {p}")
  return parse_lit_reproducer_text(p.read_text(), file=str(p), source=str(p))


def parse_lit_reproducer_text(
  text: str, *, file: str | None = None, source: str = "<text>"
) -> tuple[Reproducer, str]:
  """Parse the directives + IR from in-memory reproducer text.

  ``source`` is used only in error messages. ``file`` is the path stored on
  the returned :class:`Reproducer`; defaults to ``source`` when omitted.
  Useful for callers that already have the text (e.g. agent submissions)
  without an on-disk path.
  """
  bug_lines: list[str] = []
  run_lines: list[str] = []
  for line in text.splitlines():
    m = _BUG_RE.match(line)
    if m:
      bug_lines.append(m.group(1).strip().lower())
      continue
    m = _RUN_RE.match(line)
    if m:
      run_lines.append(m.group(1))

  if not bug_lines:
    raise ValueError(
      f"{source}: no `; BUG:` directive found. "
      f"Add a line like `; BUG: crash` (one of: "
      f"{', '.join(sorted(SUPPORTED_BUG_TYPES))})."
    )
  bug_type = bug_lines[0]
  if bug_type not in SUPPORTED_BUG_TYPES:
    raise ValueError(
      f"{source}: unsupported bug type {bug_type!r}. "
      f"Supported: {', '.join(sorted(SUPPORTED_BUG_TYPES))}."
    )

  if not run_lines:
    raise ValueError(
      f"{source}: no `; RUN:` directive found. "
      f"Add a line like `; RUN: opt -passes=instcombine -S < %s`."
    )

  command = _sanitize_run_command(run_lines[0])
  if not command:
    raise ValueError(f"{source}: `; RUN:` line is empty after sanitization.")
  tool = command.split()[0].rsplit("/", 1)[-1]
  if tool != "opt":
    raise ValueError(
      f"{source}: only `opt` is supported in ad-hoc mode; got {tool!r}. "
      f"Other tools (lli, llc, clang, …) are not yet supported."
    )

  reproducer = Reproducer(
    file=file or source,
    commands=[command],
    tests=[{"test_name": "adhoc", "test_body": text}],
  )
  return reproducer, bug_type
