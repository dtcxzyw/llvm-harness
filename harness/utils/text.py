"""Text processing utilities — keyword extraction, tokenization, matching."""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Stop words
# ---------------------------------------------------------------------------

STOP_WORDS = frozenset({
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "have", "has", "had", "do", "does", "did", "will", "would", "could",
  "should", "may", "might", "shall", "can", "to", "of", "in", "for",
  "on", "with", "at", "by", "from", "as", "into", "through", "during",
  "before", "after", "it", "its", "this", "that", "these", "those",
  "and", "but", "or", "not", "if", "when", "then", "than", "so", "no",
  "all", "each", "every", "both", "few", "more", "most", "other", "some",
  "such", "only", "same", "also", "just", "because", "about",
})  # fmt: skip


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------


def extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
  """Extract unique, non-stop-word keywords from *text*.

  Returns up to *max_keywords* words in order of first appearance.
  Words shorter than 3 characters are skipped.
  """
  words = re.findall(r"[A-Za-z_][A-Za-z0-9_]+", text)
  seen: set[str] = set()
  keywords: list[str] = []
  for w in words:
    low = w.lower()
    if low not in STOP_WORDS and low not in seen and len(low) > 2:
      seen.add(low)
      keywords.append(w)
      if len(keywords) >= max_keywords:
        break
  return keywords


# ---------------------------------------------------------------------------
# Query tokenization
# ---------------------------------------------------------------------------


def tokenize_query(query: str) -> list[str]:
  """Tokenize a search query into lowercase terms.

  Accepts identifiers with hyphens and underscores (e.g., ``flag-propagation``,
  ``inst_combine``). Single-character tokens are dropped.
  """
  return [
    w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", query) if len(w) > 1
  ]


# ---------------------------------------------------------------------------
# Term matching
# ---------------------------------------------------------------------------


def either_contains(query_term: str, keyword: str) -> bool:
  """True if either string contains the other (both sides assumed lowercase)."""
  return query_term in keyword or keyword in query_term


# ---------------------------------------------------------------------------
# Temp-file write
# ---------------------------------------------------------------------------


def write_temp_file(
  content: str,
  *,
  suffix: str = "",
  prefix: str = "tmp_",
) -> Path:
  """Write *content* to a uniquely-named temp file and return its :class:`Path`.

  Robust against partial writes (uses :meth:`Path.write_text`, which loops
  internally). The file is created via :func:`tempfile.mkstemp` so it is
  unique and owner-only (mode ``0o600``); the close-then-rewrite has no
  meaningful race.
  """
  fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
  os.close(fd)
  path = Path(path)
  path.write_text(content, encoding="utf-8")
  return path


# ---------------------------------------------------------------------------
# Oversize-text spill
# ---------------------------------------------------------------------------


def spill_if_too_long(
  content: str,
  *,
  file_path: Optional[str] = None,
  file_prefix: str = "spill_",
  char_limit: int = 15000,
  line_limit: Optional[int] = 500,
) -> str:
  """Return *content* unchanged when small; spill to a file and return a
  head/tail preview + the file path when it exceeds either limit.

  Used to keep oversize text (verbose tool-call output, ``opt --debug-only=…``
  logs, etc.) out of the agent's context window while still leaving the agent
  the first/last *char_limit/2* characters and a pointer it can ``read`` on
  demand for the middle.

  When ``file_path`` is provided the content is written there (overwriting
  any existing file — fine for fingerprint-keyed caches). When ``file_path``
  is ``None`` a tempfile is created with the given ``file_prefix``.

  Set ``line_limit=None`` to gate on character count only.
  """
  over_chars = len(content) > char_limit
  over_lines = line_limit is not None and content.count("\n") + 1 > line_limit
  if not (over_chars or over_lines):
    return content
  if file_path is None:
    file_path = str(write_temp_file(content, suffix=".txt", prefix=file_prefix))
  else:
    Path(file_path).write_text(content, encoding="utf-8")
  half = char_limit // 2
  return (
    f"{content[:half]}\n"
    f"...[output truncated, full output saved to {file_path}]...\n"
    f"{content[-half:]}"
  )
