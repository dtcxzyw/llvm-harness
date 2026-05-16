"""Pull and normalize reproducers from the ``dtcxzyw/llvm-autoreduce`` tracker.

An autoreduce "issue ID" is a GitHub issue number in
https://github.com/dtcxzyw/llvm-autoreduce. The issue body / comments embed
the reduced reproducer (IR + opt command) in human-readable form. We:

1. Fetch the issue and its comments via the GitHub REST API.
2. Hand the raw text to an LLM with a strict system prompt that asks for a
   self-contained ``.ll`` file in our embedded-directive format
   (``; BUG: …`` + ``; RUN: opt …`` + IR module).
3. Write the result to ``/tmp`` and hand the path back so the caller can
   reuse the ``--reproducer`` path through :func:`Harness.from_reproducer`.
"""

from __future__ import annotations

import os
import tempfile
from collections import namedtuple
from pathlib import Path
from typing import Tuple

from github import Github, GithubException

from harness.llvm.intern import llvm as llvm_ops
from harness.llvm.issue import SUPPORTED_BUG_TYPES, parse_lit_reproducer_text
from harness.lms.agent import AgentConfig, AgentHooks
from harness.lms.tool import (
  FuncToolCallException,
  FuncToolSpec,
  StatelessFuncToolBase,
)

AUTOREDUCE_REPO = "dtcxzyw/llvm-autoreduce"

_SYSTEM_PROMPT = """\
Parse the following issue report into a self-contained LLVM IR (.ll)
reproducer file that includes reproduction commands and the IR module.

Output TWO things
1. The .ll file content — with NO markdown fences, NO prose before or \
   after. The file MUST follow the exact format below.
2. The LLVM, alive2, and llubi's commits.

## Format

```llvm
; BUG: crash
; RUN: opt <flags> < %s
<IR module>
```

For example:

```llvm
; BUG: crash
; RUN: opt -S --passes=slp-vectorizer -mtriple=s390x-unknown-linux-gnu -mcpu=z16 -slp-threshold=-10 < %s
define i1 @test(i64 %0, i64 %1, ptr %2) {
;
entry:
  %gep44 = getelementptr i8, ptr null, i64 %0
  %gep45 = getelementptr i8, ptr null, i64 %1
  %4 = icmp ult ptr %gep44, %gep45
  %umin = select i1 %4, ptr %gep44, ptr %gep45
  %gep48 = getelementptr i8, ptr null, i64 %0
  %gep49 = getelementptr i8, ptr null, i64 %1
  %5 = icmp ult ptr %gep48, %gep49
  %umin50 = select i1 %5, ptr %gep48, ptr %gep49
  %b095 = icmp ult ptr %umin, %2
  %b196 = icmp ult ptr %umin50, %2
  %res = and i1 %b095, %b196
  ret i1 %res
}
```

## Rules

- The first non-blank line MUST be `; BUG: crash` or `; BUG: miscompilation`.
- The second non-blank line MUST be a `; RUN:` line whose command starts \
with `opt` and uses `%s` as the input-file placeholder and uses `-S` to \
emit LLVM IR in text form for miscompilations since we will use alive2.
- The rest of the file is the LLVM IR module verbatim — no markdown fences \
(no triple backticks), no prose, no commentary.
- If the issue contains multiple reproducers, pick the smallest one that \
unambiguously demonstrates the bug.
- Strip any `| FileCheck …` pipelines or shell-redirection tails from the \
RUN command (keep `< %s`, drop everything from the first `|`).
- Strip leading lit wrappers like `not`, `not --crash`, or `env VAR=val` \
from the RUN command.
- If the issue uses `opt -O2`/`-O3`-style flags rather than `-passes=…`, \
keep them as-is.
"""

ReprodInfo = namedtuple(
  "ReprodInfo", ["llvm_commit", "alive2_commit", "llubi_commit", "content"]
)


def _fetch_issue_text(issue_id: str) -> str:
  """Fetch an autoreduce issue's body via PyGithub (anonymous).

  Anonymous calls are rate-limited to 60 requests/hour per IP, which is
  enough for the CLI's interactive use; ``autofix.ghbot`` runs as a
  GitHub App and never touches this path. Raises ``RuntimeError`` on any
  API failure with a clear message.
  """
  try:
    # TODO: Add GH_TOKEN and GITHUB_TOKEN back to allow access private repos
    repo = Github().get_repo(AUTOREDUCE_REPO)
    issue = repo.get_issue(int(issue_id))
  except GithubException as e:
    raise RuntimeError(
      f"GitHub API request failed for issue {issue_id}: {e.status} {e.data}"
    )
  return issue.body or ""


def _strip_markdown_fence(text: str) -> str:
  """If the model wrapped its output in ``` fences despite the system prompt,
  strip them. Tolerates language hints (```llvm, ```ll)."""
  t = text.strip()
  if not t.startswith("```"):
    return t
  first_nl = t.find("\n")
  if first_nl == -1:
    return t
  t = t[first_nl + 1 :]
  end = t.rfind("```")
  if end != -1:
    t = t[:end]
  return t


class _SubmitReproducerTool(StatelessFuncToolBase):
  """Single tool the autoreduce-parser agent uses to hand back the
  normalized ``.ll`` content + the commits the reproducer was reduced on.

  Validation delegates to :func:`parse_lit_reproducer_text` so the BUG/RUN
  rules stay in one place. On success the captured fields are written into
  the ``submission`` dict supplied at construction; the agent loop stops on
  the first successful call via :class:`AgentHooks` ``post_tool_call``.
  """

  def __init__(self, submission: dict):
    self._submission = submission

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "submit_reproducer",
      "Submit the parsed reproducer as a self-contained .ll file and the "
      "LLVM/Alive2/LLUBI commits it was reduced against. Content MUST start "
      "with `; BUG: " + "|".join(sorted(SUPPORTED_BUG_TYPES)) + "`, "
      "followed by a `; RUN: opt ... < %s` line, then the IR module. "
      "No markdown fences, no prose.",
      [
        FuncToolSpec.Param(
          "content",
          "string",
          True,
          "The full .ll file content (no markdown fences). "
          "First non-blank line: `; BUG: <"
          + "|".join(sorted(SUPPORTED_BUG_TYPES))
          + ">`. "
          "Second non-blank line: `; RUN: opt ... < %s`. "
          "Then the IR module verbatim.",
        ),
        FuncToolSpec.Param(
          "llvm_commit",
          "string",
          True,
          "The LLVM commit hash this reproducer applies to.",
        ),
        FuncToolSpec.Param(
          "alive2_commit",
          "string",
          True,
          "The Alive2 commit hash this reproducer applies to.",
        ),
        FuncToolSpec.Param(
          "llubi_commit",
          "string",
          True,
          "The LLUBI commit hash this reproducer applies to.",
        ),
      ],
      [],
    )

  def _call(
    self,
    *,
    content: str,
    llvm_commit: str,
    alive2_commit: str,
    llubi_commit: str,
    **kwargs,
  ) -> str:
    for label, val in (
      ("content", content),
      ("llvm_commit", llvm_commit),
      ("alive2_commit", alive2_commit),
      ("llubi_commit", llubi_commit),
    ):
      if not val or not val.strip():
        raise FuncToolCallException(f"{label} must not be empty")
    text = _strip_markdown_fence(content).strip()
    try:
      repro, bug_type = parse_lit_reproducer_text(text, source="<submission>")
    except ValueError as e:
      raise FuncToolCallException(str(e))
    # Miscompilation reproducers feed opt's stdout into alive2; without
    # ``-S`` opt emits bitcode and downstream decoding fails on the magic
    # bytes. Reject here so the agent retries with a corrected command
    # rather than producing a broken reproducer.
    if bug_type == "miscompilation":
      for cmd in repro.commands:
        if "opt" in cmd.split() and "-S" not in cmd.split():
          raise FuncToolCallException(
            f"miscompilation RUN command must include `-S` to emit textual "
            f"LLVM IR (got: {cmd!r}). Add `-S` and resubmit."
          )
    try:
      resolved_llvm = llvm_ops.git_execute(["rev-parse", llvm_commit.strip()]).strip()
      # TODO: resolve alive2 and llubi
    except Exception:
      raise FuncToolCallException(
        f"llvm_commit {llvm_commit!r} is not a valid commit hash in the LLVM repo"
      )
    self._submission.update(
      content=text,
      llvm_commit=resolved_llvm,
      alive2_commit=alive2_commit.strip(),
      llubi_commit=llubi_commit.strip(),
    )
    return "Reproducer submitted successfully."


def _normalize_with_llm(raw: str, *, agent_config: AgentConfig) -> ReprodInfo:
  """Run a single-tool agent loop and return the submitted reproducer info."""
  submission: dict = {}
  agent = agent_config.create_agent(
    tools=[(_SubmitReproducerTool(submission), 5)],
  )

  agent.append_system_message(_SYSTEM_PROMPT)
  agent.append_user_message(raw)

  def post_response(_: str):
    return True, (
      "Error: please call the `submit_reproducer` tool with the parsed "
      "reproducer content; do not reply with prose."
    )

  def post_tool_call(name: str, _args: str, result: str):
    if name == "submit_reproducer" and submission:
      return False, result  # validation passed; stop the loop
    return True, result

  agent.run(AgentHooks(post_response=post_response, post_tool_call=post_tool_call))
  if not submission:
    raise RuntimeError("autoreduce agent finished without submitting a reproducer")
  return ReprodInfo(**submission)


def fetch_autoreduce_reproducer(
  issue_id: str, *, agent_config: AgentConfig
) -> Tuple[Path, ReprodInfo]:
  """Fetch and normalize an autoreduce issue into a `.ll` reproducer file.

  Returns the path to a temporary ``.ll`` file under ``/tmp`` and related
  information. Raises ``RuntimeError`` when the issue is empty or doesn't
  contain an LLVM IR block.
  """
  raw = _fetch_issue_text(issue_id)
  if not raw.strip() or "```llvm" not in raw:
    raise RuntimeError(
      f"autoreduce issue {issue_id} doesn't contain an ```llvm code block"
    )
  rinfo = _normalize_with_llm(raw, agent_config=agent_config)
  fd, path = tempfile.mkstemp(suffix=".ll", prefix=f"autored_{issue_id}_")
  try:
    os.write(fd, rinfo.content.encode("utf-8"))
  finally:
    os.close(fd)
  return Path(path), rinfo


def main() -> int:
  import argparse
  import sys

  from autofix.mini import build_agent_config

  parser = argparse.ArgumentParser(
    description=(
      "Fetch a GitHub issue from dtcxzyw/llvm-autoreduce and normalize it "
      "into a self-contained `; BUG:` / `; RUN:` reproducer file when --model is provided"
    ),
  )
  parser.add_argument("issue", help="GitHub issue number in dtcxzyw/llvm-autoreduce")
  parser.add_argument(
    "--model",
    default=None,
    help="LLM model used to normalize the issue (required unless --raw).",
  )
  parser.add_argument(
    "--driver",
    default="openai",
    choices=["openai", "anthropic"],
    help="LLM API driver (default: openai).",
  )
  parser.add_argument(
    "--verbose",
    action="store_true",
    help="Enable verbose debug output from the LLM agent (default: False).",
  )
  args = parser.parse_args()

  try:
    raw = _fetch_issue_text(args.issue)
  except RuntimeError as e:
    print(f"Error: {e}", file=sys.stderr)
    return 1

  if not raw.strip() or "```llvm" not in raw:
    print(
      f"Error: autoreduce issue {args.issue} doesn't contain an ```llvm code block",
      file=sys.stderr,
    )
    return 1

  if not args.model:
    print(raw)
    return 0

  agent_config = build_agent_config(args.driver, args.model, args.verbose)
  try:
    rinfo = _normalize_with_llm(raw, agent_config=agent_config)
  except Exception as e:
    print(f"Error: normalization failed: {e}", file=sys.stderr)
    return 1

  print(f"LLVM   commit: {rinfo.llvm_commit}")
  print(f"Alive2 commit: {rinfo.alive2_commit}")
  print(f"LLUBI  commit: {rinfo.llubi_commit}")
  print("---")
  print(rinfo.content)
  return 0


if __name__ == "__main__":
  import sys

  sys.exit(main())
