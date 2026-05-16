import json
import os
import re
import time
from argparse import ArgumentParser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import yaml

import harness
from harness.llvm import (
  AccessControl,
  Harness,
  ReprodRes,
)
from harness.llvm.debugger import DebuggerBase, StackTrace
from harness.lms.agent import AgentBase, AgentConfig, AgentHooks
from harness.lms.meter import GlobalMeter
from harness.lms.tool import (
  FuncToolBase,
  FuncToolCallException,
  FuncToolSpec,
  StatelessFuncToolBase,
)
from harness.tools.subagent import SubAgentTool
from harness.tools.todo import TodoTool
from harness.utils.console import get_boxed_console
from harness.utils.text import spill_if_too_long

# - ===============================================
# - Prompts
# - ===============================================

_PROMPTS = harness.load_yaml_config("autofix", "mini.yaml")["prompts"]
PROMPT_REASON = _PROMPTS["reason"]
PROMPT_REPAIR = _PROMPTS["repair"]

# - ===============================================
# - Agent configurations
# - ===============================================

# We restrict the agent to chat at most 500 rounds for each run
# and consume at most 5 million tokens among all runs.
AGENT_TEMPERATURE = 0  # 0.8
AGENT_TOP_P = 0.95
AGENT_MAX_COMPLETION_TOKENS = 8092
AGENT_REASONINT_EFFORT = "NOT_GIVEN"  # "xhigh"
AGENT_MAX_CHAT_ROUNDS = 500  # 1000
AGENT_MAX_CONSUMED_TOKENS = 5_000_000  # 10_000_000
# We give context gathering tools more budget and restrict the models
# to be careful and think twice when they are editing and testing.
MAX_TCS_LIGHTWEIGHT_TOOLS = 250
MAX_TCS_HEAVYWEIGHT_TOOLS = 25  # 250
MIN_EDITPOINT_LINES = 3
# Enabled tools per stage and their categories
ENABLED_REASON_TOOLS = {
  # Explore codebase tools
  "list",
  "read",
  "find",
  "ripgrep",
  "llvm_code",
  # Documentation tools
  "llvm_docs",
  "llvm_langref",
  # Debugging tools
  "llvm_debug",
  "llvm_eval_expr",
  # Execution tools
  "llvm_interpret_ir",
  "llvm_interpret_ir_legacy",
  "llvm_execute_ir",
  # Subagent tools
  "subagent",
  # Knowledge tools
  "insight",
  # Interaction tools
  "ask",  # Enabled when --interactive
  # Report tool to finish the analysis
  "submit_analysis",
}
ENABLED_REPAIR_TOOLS = {
  # Explore codebase tools
  "list",
  "read",
  "find",
  "ripgrep",
  "llvm_code",
  "bash",
  "write",
  # Documentation tools
  "llvm_docs",
  "llvm_langref",
  # Edit tools
  "edit",
  # Plan tools
  "todo",
  # Subagent tools
  "subagent",
  # Knowledge tools
  "insight",
  # Build & test tools
  "llvm_build",
  "llvm_reset",
  "llvm_test",
  "llvm_preview_patch",
  "llvm_interpret_ir",
  "llvm_interpret_ir_legacy",
  "llvm_execute_ir",
  "llvm_optimize_ir",
  "llvm_compile_ir",
  "llvm_verify_ir",
  "llvm_check_optim",
  "llvm_verify_optim",
  # Interaction tools
  "ask",  # Enabled when --interactive
  # Report tool to submit a patch report
  "submit_patchreport",
}
ENABLED_CURATE_INSIGHT_TOOLS = {"read", "ripgrep", "insight"}
ALL_ENABLED_TOOLS = (
  ENABLED_REASON_TOOLS | ENABLED_REPAIR_TOOLS | ENABLED_CURATE_INSIGHT_TOOLS
)
HEAVYWEIGHT_TOOLS = {"llvm_test", "subagent"}
LIGHTWEIGHT_TOOLS = ALL_ENABLED_TOOLS - HEAVYWEIGHT_TOOLS
# Enabled skills per stage and their categories
ENABLED_REASON_SKILLS = {"llvm-insight-search"}
ENABLED_REPAIR_SKILLS = {"llvm-patchreview", "llvm-insight-search"}
ENABLED_CURATE_INSIGHT_SKILLS = {"llvm-insight-reflect"}
ALL_ENABLED_SKILLS = (
  ENABLED_REASON_SKILLS | ENABLED_REPAIR_SKILLS | ENABLED_CURATE_INSIGHT_SKILLS
)
HAS_REVIEW_SKILL = "llvm-patchreview" in ENABLED_REPAIR_SKILLS
# Deferred tools — any tool in any stage is deferred if listed here.
# These get stub descriptions; the agent uses tool_search to load full specs.
DEFERRED_TOOLS = {
  "insight",
  "llvm_docs",
  "llvm_langref",
  "llvm_code",
  "llvm_interpret_ir",
  "llvm_interpret_ir_legacy",
  "llvm_execute_ir",
  "llvm_optimize_ir",
  "llvm_compile_ir",
  "llvm_verify_ir",
  "llvm_check_optim",
  "llvm_verify_optim",
  "llvm-patchreview",
  "llvm-insight-search",
}

# - ================================================
# - LLVM settings
# - ================================================

ASSERTION_FUNCTION_LIST = [
  "__assert_fail",
  "__GI___assert_fail",
  "llvm::llvm_unreachable_internal",
  "llvm::report_fatal_error",
]

# NOTE: Patterns start with star will be passed into rbreak.
# FIXME: rbreak is slow. Use grep instead?
TRANSFORMATION_FUNCTION_LIST = [
  "Instruction::clone",
  "Instruction::replaceSuccessorWith",
  "Instruction::setSuccessor",
  "User::setOperand",
  "::eraseFromParent",
  "Use::set",
  "Use::operator=",
  "User::replaceUsesOfWith",
  "Value::replaceAllUsesWith",
  "InstCombiner::InsertNewInstBefore",
  "InstCombiner::InsertNewInstWith",
  "InstCombiner::replaceInstUsesWith",
  "InstCombiner::replaceOperand",
  "InstCombiner::replaceUse",
  "SwitchInst::addCase",
  "SwitchInst::removeCase",
  "BinaryOperator::swapOperands",
  "BranchInst::swapSuccessors",
  "BranchInst::setCondition",
  "BranchInst::setSuccessor",
  "SwitchInst::setCondition",
  "SwitchInst::setDefaultDest",
  "SwitchInst::setSuccessor",
  "CmpInst::swapOperands",
  "ICmpInst::swapOperands",
  "FCmpInst::swapOperands",
  "CmpInst::setPredicate",
  "PHINode::addIncoming",
  "PHINode::setIncomingValue",
  "PHINode::addIncoming",
  "*Inst::Create",
  "*Inst::operator new",
  "*BinaryOperator::Create",
  "*IRBuilderBase::Create",
]

COMPILATION_FLAGS = "-O0 -ggdb"
ADDITIONAL_CMAKE_FLAGS = [
  f"-DCMAKE_C_FLAGS_RELWITHDEBINFO={COMPILATION_FLAGS}",
  f"-DCMAKE_CXX_FLAGS_RELWITHDEBINFO={COMPILATION_FLAGS}",
]

# - ================================================
# - Statistis and output
# - ================================================

console = get_boxed_console(debug_mode=False)


def panic(msg: str):
  console.print(f"Error: {msg}", color="red")
  exit(1)


@dataclass
class RunStats:
  # Command to run autofix
  command: dict
  # The generated path for successful runs
  patch: Optional[str] = None
  # The error message for failed runs
  error: Optional[str] = None
  errmsg: Optional[str] = None
  traceback: Optional[str] = None
  # Agent interaction stats
  input_tokens: int = 0
  output_tokens: int = 0
  cached_tokens: int = 0
  total_tokens: int = 0
  chat_rounds: int = 0
  total_time_sec: float = 0.0
  # Fix stats
  opt_pass: str = "<not-provided>"
  trans_point: Tuple[str, str] = ("<not-provided>", "<not-provided>")
  editpoints: List[Tuple[str, int, int]] = field(
    default_factory=lambda *_, **__: [("<not-provided>", -1, -1)]
  )
  reasoning: str = "<not-provided>"
  test_traj: List[Tuple[str, bool]] = field(
    default_factory=list
  )  # Trajectories of (patch, passed?) for each test call
  rev_traj: List[Tuple[str, str]] = field(
    default_factory=list
  )  # Trajectories of (patch, verdict) for each review call; verdict is APPROVE/REVISE/REJECT/UNKNOWN
  patch_report: Optional[str] = (
    None  # Final patch report (root cause + fix explanation)
  )

  def as_dict(self) -> dict:
    return asdict(self)


# - ===============================================
# - Agent's main code
# - ==============================================


class NoAvailablePatchFound(Exception):
  pass


class ReachToolBudget(Exception):
  pass


@dataclass
class PatchEditPoint:
  """A class to represent an edit point in a patch."""

  start: int
  end: int
  file: Path

  def as_tuple(self) -> Tuple[str, int, int]:
    return (str(self.file), self.start, self.end)

  def __str__(self) -> str:
    return f"{self.file}:{self.start}-{self.end}"


def ensure_tools_available(agent: AgentBase, tools: List[str]):
  available_tools = agent.tools.list(ignore_budget=False)
  unavailable_tools = []
  for tool in tools:
    if tool not in available_tools:
      unavailable_tools.append(tool)
  if len(unavailable_tools) > 0:
    raise ReachToolBudget(f"Tools [{', '.join(unavailable_tools)}] are out of budget.")


_EDITPOINT_FORMAT = """\
```cpp
// {file}:{start}-{end}
{code}
```\
"""

# Review verdicts
_VERDICT_APPROVE = "APPROVE"
_VERDICT_REVISE = "REVISE"
_VERDICT_REJECT = "REJECT"
_VERDICT_UNKNOWN = "UNKNOWN"
_VALID_VERDICTS = {_VERDICT_APPROVE, _VERDICT_REVISE, _VERDICT_REJECT}


def _parse_review_verdict(report: str) -> Optional[str]:
  """Parse the verdict from a review report's YAML frontmatter.

  Returns the verdict string (APPROVE/REVISE/REJECT) or None if the
  report is not valid Markdown with YAML frontmatter.
  """
  report = report.strip()
  start = report.find("---")
  if start == -1:
    return None  # No frontmatter start found
  end = report.find("---", start + 3)
  if end == -1:
    return None  # No frontmatter end found
  try:
    header = yaml.safe_load(report[start + 3 : end])
    if not isinstance(header, dict):
      return None  # Invalid frontmatter format
    verdict = str(header.get("verdict", "")).strip().upper()
    if verdict in _VALID_VERDICTS:
      return verdict
    return None  # Unknown verdict value
  except Exception:
    return None  # Invalid YAML format


def patch_and_fix(
  editpoints: List[PatchEditPoint],
  reason_info: str,
  *,
  rep: ReprodRes,
  aconf: AgentConfig,
  harness: Harness,
  stats: RunStats,
  interactive: bool = False,
) -> Optional[str]:
  fixenv = harness.fixenv
  console.print(
    f"Generating patch for edit points: {', '.join([str(ep) for ep in editpoints])} ..."
  )

  # Reset the LLVM repo to the base commit
  harness.git("checkout", ".")
  agent = _create_repair_agent(aconf, harness, interactive=interactive)

  # Fix: There're chances that the model proposes incorrect edit points
  formatted_editpoints = []
  for ep in editpoints:
    try:
      formatted_editpoints.append(
        _EDITPOINT_FORMAT.format(
          file=ep.file,
          start=ep.start,
          end=ep.end,
          code=harness.llvmcode.extract_snippet(
            str(ep.file),
            ep.start,
            ep.end,
            context=5,
          ),
        )
      )
    except ValueError as e:
      console.print(
        f"Warning: Skip: Failed to extract code snippet for edit point {ep}: {e}",
        color="yellow",
      )

  # Generate the patch according to the information and proposed edit points
  agent.append_user_message(
    PROMPT_REPAIR.format(
      reprod_code=rep.file_path.read_text(),
      issue_symptom=rep.symptom,
      reason_info=reason_info,
      editpoints="\n".join(formatted_editpoints) or "<not-found>",
      pass_name=stats.opt_pass.lower().replace(" ", "-"),
    )
  )

  # The model drives the repair-review cycle autonomously:
  # it edits, tests, calls llvm-patchreview, revises if needed, and
  # only submits once the reviewer approves.

  def post_response(_: str) -> Tuple[bool, str]:
    ensure_tools_available(agent, ["llvm_test", "edit"])
    return True, (
      "Error: You are not calling any tool or your tool call format is incorrect. "
      "You should always continue with tool calling and correct tool call format. "
      "Please continue."
      " If you are done, call the `llvm_test` tool to see if it passes the tests."
      " If you already called the `llvm_test` tool, please check the feedback, adjust the patch, and try again."
    )

  def _latest_test_passed() -> bool:
    return bool(stats.test_traj) and stats.test_traj[-1][1]

  def _latest_review_approved() -> bool:
    """True only if the last review approved the current patch."""
    if not stats.rev_traj:
      return False
    rev_patch, verdict = stats.rev_traj[-1]
    cur_patch = fixenv.dump_patch()
    return verdict == _VERDICT_APPROVE and rev_patch == cur_patch

  def pre_tool_call(name: str, args: dict) -> Tuple[bool, dict | str]:
    if name == "llvm-patchreview" and not _latest_test_passed():
      return (
        False,
        "Error: cannot request review — "
        "the latest test did not pass or you haven't tested yet. "
        "If you haven't tested, please call the `llvm_test` tool first. "
        "If you have tested, please check the feedback, adjust the patch, and try again.",
      )  # Stop the tool call; feed the response back to the model
    return True, args  # Continue the tool call with the original args

  def post_tool_call(name: str, _: str, res: str) -> Tuple[bool, str]:
    ensure_tools_available(agent, ["llvm_test", "edit"])
    if name == "llvm_test":
      patch = fixenv.dump_patch()
      passed = res == "<success>"
      stats.test_traj.append((patch, passed))
      # New test invalidates prior review (rev_traj tracks patch-verdict pairs,
      # so _latest_review_approved() naturally returns False after a new test
      # only if the review was for a different patch).
      if passed and not HAS_REVIEW_SKILL:
        return False, patch  # No review skill — stop on test success
    elif name == "llvm-patchreview":
      verdict = _parse_review_verdict(res) or _VERDICT_UNKNOWN
      stats.rev_traj.append((fixenv.dump_patch(), verdict))
    elif name == "submit_patchreport":
      if not _latest_test_passed():
        return (
          True,
          "Error: cannot submit — "
          "the latest test did not pass or you didn't test. "
          "If you haven't tested, please call the `llvm_test` tool first. "
          "If you have tested, please check the feedback, adjust the patch, and try again.",
        )  # Contiue the process but with this error message to guide the model
      if HAS_REVIEW_SKILL and not _latest_review_approved():
        return (
          True,
          "Error: cannot submit — "
          "the latest review did not approve the patch or "
          "you didn't apply for patch review for the latest patch. "
          "If you haven't applied for review, please call the `llvm-patchreview` tool first. "
          "If you have applied for review, please check the feedback, adjust the patch, and try again.",
        )  # Contiue the process but with this error message to guide the model
      stats.patch_report = res
      return False, fixenv.dump_patch()  # Stop the process and return the final patch
    return True, res  # Continue the process for other tools

  return agent.run(
    AgentHooks(
      post_response=post_response,
      post_tool_call=post_tool_call,
      pre_tool_call=pre_tool_call,
    ),
  )


class SubmitAnalysisTool(StatelessFuncToolBase):
  def __init__(self, acl: AccessControl, min_editpoint_lines: int):
    self.acl = acl
    self.min_editpoint_lines = min_editpoint_lines

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "submit_analysis",
      "Stop analysis and report the found edit points for fixing the issue",
      [
        FuncToolSpec.Param(
          "editpoints",
          "list[tuple[int,int,string]]",
          True,
          "A list of edit points with each being a tuple of the one-indexed starting line number (included)"
          ", the ending line number (included), and the absolute path of the file to edit.",
        ),
        FuncToolSpec.Param(
          "thoughts",
          "string",
          True,
          'The detailed thoughts for diagnosing the issue including step-by-step "'
          '1. Understanding the Issue", '
          '2. "Analyzing `opt`\'s Log", '
          '3. "Root Cause Analysis", '
          '4. "Proposed Edit Points(s)", and '
          '5. "Conclusion".',
        ),
      ],
      [],
    )

  def _call(
    self, *, editpoints: list[tuple[int, int, str]], thoughts: str, **kwargs
  ) -> str:
    # Check and fix the model-provided edit points
    fixed_editpoints = []
    for ind, edit in enumerate(editpoints):
      if len(edit) != 3:
        raise FuncToolCallException(
          f"Each edit point must be a tuple of 3 elements (starting line number, ending line number, and the absolute path of the file to edit): {edit}"
        )
      fixed_edit = []
      try:
        start_line = int(edit[0])
      except Exception:
        raise FuncToolCallException(
          f"The starting line number must be an integer, got {edit[0]} at editpoints[{ind}]: {edit}"
        )
      if start_line < 1:
        raise FuncToolCallException(
          f"The starting line number must be an one-indexed integer, got {start_line} at editpoints[{ind}]: {edit}"
        )
      fixed_edit.append(start_line)
      try:
        end_line = int(edit[1])
      except Exception:
        raise FuncToolCallException(
          f"The ending line number must be an integer, got {edit[1]} at editpoints[{ind}]: {edit}"
        )
      if end_line < 1:
        raise FuncToolCallException(
          f"The ending line number must be an one-indexed integer, got {end_line} at editpoints[{ind}]: {edit}"
        )
      if end_line - start_line + 1 < self.min_editpoint_lines:
        raise FuncToolCallException(
          f"An edit point must be at least {self.min_editpoint_lines} lines long, got {end_line - start_line + 1} lines at editpoints[{ind}]: {edit}"
        )
      fixed_edit.append(end_line)
      try:
        resolved = self.acl.check_readable_file(edit[2])
        fixed_edit.append(str(resolved))
      except Exception as e:
        raise FuncToolCallException(
          f"Invalid file path for detected at editpoints[{ind}]: {edit}. {e}"
        )
      fixed_editpoints.append(tuple(fixed_edit))
    return json.dumps(
      {
        "editpoints": fixed_editpoints,
        "thoughts": thoughts,
      },
      indent=2,
    )


class SubmitPatchReportTool(StatelessFuncToolBase):
  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "submit_patchreport",
      "Submit a patch report explaining the original bug's root cause and how it was fixed in the patch. "
      "Call this only after the patch has been approved by the reviewer.",
      [
        FuncToolSpec.Param(
          "report",
          "string",
          True,
          'A concise report in Markdown titled "Patch Report" covering three sections:'
          "(1) Overview: an overview of the issue, the root cause, and the fix; "
          "(2) Root Cause Analysis: the original bug and its root cause; "
          "(3) Fix Explanation: how the patch fixes it, and why the fix is correct. "
          "(4) Experiences: experiences (and pitfalls/patterns) gained from the bug and the fix. ",
        ),
      ],
      [],
    )

  def _call(self, *, report: str, **kwargs) -> str:
    if not report or not report.strip():
      raise FuncToolCallException("The patch report must not be empty.")
    return report


def run_mini_agent(
  rep: ReprodRes,
  *,
  # Opt information
  opt_pass: str,
  opt_cmd: str,
  opt_log: str,
  # Debugger information
  debugger: DebuggerBase,
  backtrace: StackTrace,
  # Agent config
  aconf: AgentConfig,
  # Harness
  harness: Harness,
  # Statistics
  stats: RunStats,
  # Interactive mode
  interactive: bool = False,
) -> Optional[str]:
  #####################################################
  # The agent runs by:
  # 1. Analyze the issue first to reason about the root cause and propose potential edit points.
  # 2. Leverage the provided information to guide the patch generation.
  #####################################################

  # Reason about the root cause and propose potential edit points
  console.print("Analyzing the issue to gather required information ...")
  reason_agent = _create_reason_agent(aconf, harness, interactive=interactive)
  # ``opt --debug-only=…`` can be hundreds of KB. Spill to a fingerprint-keyed
  # file in the build dir if it'd blow the context window; the agent can
  # ``read`` the rest on demand.
  opt_log_for_prompt = spill_if_too_long(
    opt_log,
    file_path=os.path.join(str(harness.build_dir), f"opt_log.{rep.fingerprint()}.txt"),
    char_limit=4096,
    line_limit=150,
  )
  reason_agent.append_user_message(
    PROMPT_REASON.format(
      pass_name=opt_pass,
      pass_name_lower=opt_pass.lower().replace(" ", "-"),
      reprod_code=rep.file_path.read_text(),
      issue_symptom=rep.symptom,
      opt_cmd=opt_cmd,
      opt_log=opt_log_for_prompt,
      trans_point_file=str(backtrace[-1].file),
      trans_point_func=backtrace[-1].func,
      trans_point_stack="\n".join([str(it) for it in reversed(backtrace)]),
      min_editpoint_lines=MIN_EDITPOINT_LINES,
    )
  )

  def response_handler(_: str) -> Tuple[bool, str]:
    ensure_tools_available(reason_agent, ["submit_analysis"])
    return True, (
      "Error: You are not calling any tool or your tool call format is incorrect. "
      "You should always continue with tool calling and correct tool call format. "
      "Please continue."
      " If you are done, call the `submit_analysis` tool with the edit points."
      " If you already called the `submit_analysis` tool, please check the format and try again."
    )  # Continue the process but with this error message to guide the model

  def tool_call_handler(name: str, _: str, res: str) -> Tuple[bool, str]:
    ensure_tools_available(reason_agent, ["submit_analysis"])
    if name != "submit_analysis":
      return True, res  # Continue the process
    try:
      # The report tool returns a parseable JSON string
      json.loads(res)
    except Exception:
      return (True, res)  # Continue the process with an error message
    return False, res  # Stop the process with the result

  response = reason_agent.run(
    AgentHooks(post_response=response_handler, post_tool_call=tool_call_handler),
  )

  # Parse the response to get potential edit points
  response = json.loads(response)
  editpoints = response.get("editpoints", [])
  reasoning_thoughts = response.get("thoughts", "")
  fixed_editpoints = []

  for editpoint in editpoints:
    try:
      editpoint_start, editpoint_end, editpoint_file = editpoint
      if not is_interesting_file(editpoint_file):
        console.print(f"Ignore non-interesting file {editpoint_file} for now.")
        continue
      fixed_editpoints.append(
        PatchEditPoint(int(editpoint_start), int(editpoint_end), Path(editpoint_file))
      )
    except Exception as e:
      console.print(
        f"WARNING: skip edit point {editpoint} due to parse failure: {e}",
        color="yellow",
      )

  stats.reasoning = reasoning_thoughts
  stats.editpoints = [ep.as_tuple() for ep in fixed_editpoints]

  # Generate a patch and fix the issue according to the information
  return patch_and_fix(
    fixed_editpoints,
    reasoning_thoughts,
    rep=rep,
    aconf=aconf,
    harness=harness,
    stats=stats,
    interactive=interactive,
  )


_LIT_TEST_DIR_PATTERN = re.compile(r"llvm/lib/(Transforms|Analysis)/([^/]+)/")


# Fallback when nothing in the command or backtrace fuzzy-matches an existing
# test dir — exercises the full middle-end regression set, same scope as
# ``--aggressive-testing``.
_FULL_MIDEND_LIT_DIRS = ["llvm/test/Transforms", "llvm/test/Analysis"]


def _infer_lit_test_dirs(
  harness: Harness,
  raw_command: str,
  backtrace: Optional[StackTrace] = None,
) -> List[str]:
  """Collect every lit test dir credibly implicated by the bug.

  Sources, deduplicated and unioned:

  1. Every pass name from ``-passes=...`` (so ``-passes=A,B`` contributes
     both ``test/Transforms/A`` and ``test/Transforms/B`` when they exist).
  2. When a backtrace is supplied, the topmost frame that yields a match,
     via file basename, ``lib/(Transforms|Analysis)/<X>`` directory name,
     or the curated utility-pass mapping. Scripts without a debugger (xcli,
     mswe) pass ``backtrace=None`` and rely on the command alone.

  Each candidate is fuzzy-matched against the actual ``test/Transforms/*``
  and ``test/Analysis/*`` tree, so nothing is returned unless the directory
  exists. Returns ``[]`` when no candidate matches — caller decides what to
  fall back to.
  """
  llvmcode = harness.llvmcode
  seen: set[str] = set()
  dirs: List[str] = []

  def _add(found: Optional[str]):
    if found and found not in seen:
      seen.add(found)
      dirs.append(found)

  for pass_name in llvmcode.resolve_pass_names(raw_command):
    _add(llvmcode.resolve_test_dir(pass_name))

  if backtrace is None:
    return dirs

  for frame in reversed(backtrace):
    file_path = str(frame.file)
    before = len(dirs)
    m = _LIT_TEST_DIR_PATTERN.search(file_path)
    if m:
      _add(llvmcode.resolve_test_dir(Path(file_path).stem))
      _add(llvmcode.resolve_test_dir(m.group(2)))
    utility_pass = llvmcode.resolve_utility_pass(file_path)
    if utility_pass:
      _add(llvmcode.resolve_test_dir(utility_pass))
    if len(dirs) > before:
      break  # first frame that contributed — deeper frames are noisy
  return dirs


def configure_lit_test_dirs(
  harness: Harness,
  raw_command: str,
  *,
  backtrace: Optional[StackTrace] = None,
  log=print,
  warn=None,
) -> None:
  """Set ``harness.fixenv.card.lit_test_dir`` from inference.

  No-op when it's already set (bench-curated issues). When nothing can be
  inferred, falls back to the full middle-end suite — same scope as
  ``--aggressive-testing``. ``log`` is called with a success line, ``warn``
  (defaults to ``log``) with the fallback notice.
  """
  card = harness.fixenv.card
  if card.lit_test_dir is not None:
    return
  warn = warn or log
  inferred = _infer_lit_test_dirs(harness, raw_command, backtrace)
  if inferred:
    log(f"Inferred lit_test_dir: {inferred}")
    card.lit_test_dir = inferred
  else:
    warn(
      "Warning: could not infer a narrow lit_test_dir; "
      f"falling back to full middle-end: {_FULL_MIDEND_LIT_DIRS}"
    )
    card.lit_test_dir = list(_FULL_MIDEND_LIT_DIRS)


def is_interesting_file(filename: str) -> bool:
  if "llvm/ADT" in filename or "llvm/Support" in filename:
    return False
  if filename.endswith(".cpp"):
    return True
  # This is not an always-safe operation (some bugs may happen in functions defined in header files)
  if filename.endswith(".h"):
    # Avoid modifying llvm/IR files to reduce the rebuild time.
    return "llvm/Transforms" in filename or "llvm/Analysis" in filename
  return False


_BREAKPOINT_CACHE_FILE = "autofix_breakpoints.json"


def _load_breakpoint_cache(path: str) -> dict:
  if not os.path.exists(path):
    return {}
  try:
    with open(path) as fin:
      data = json.load(fin)
    return data if isinstance(data, dict) else {}
  except (json.JSONDecodeError, OSError):
    return {}


def _save_breakpoint_cache(path: str, cache: dict) -> None:
  with open(path, "w") as fout:
    json.dump(cache, fout, indent=2, sort_keys=True)


def prepare_debugger(
  rep: ReprodRes, *, harness: Harness
) -> Tuple[DebuggerBase, StackTrace]:
  debugger = harness.attach_debugger(rep.command)

  # Pause the debugger at the first transformation point or crash point
  bug_type = harness.fixenv.get_bug_type()
  breakpints = (
    ASSERTION_FUNCTION_LIST if bug_type == "crash" else TRANSFORMATION_FUNCTION_LIST
  )
  # Bench issues get a per-issue build dir, so a single-entry cache file
  # works there. Ad-hoc reproducers (--reproducer / --autoreduce) share the
  # default build dir, so we key the cache by a hash of the reproducer's
  # IR + RUN command — same content reuses, distinct content stays isolated.
  cache_path = os.path.join(str(harness.build_dir), _BREAKPOINT_CACHE_FILE)
  cache = _load_breakpoint_cache(cache_path)
  cache_key = rep.fingerprint()
  cached_breakpoint = cache.get(cache_key)
  if cached_breakpoint:
    console.print(f"Using the cached breakpoint function: {cached_breakpoint}")
    breakpints = [cached_breakpoint]
  console.print("Reproducing the issue with debugger...")
  backtrace, breakpoint = debugger.run(
    harness.llvm_dir,
    breakpints,
    bug_type == "miscompilation",
    frame_limit=25,  # 25 frames should be enough
  )
  if not cached_breakpoint and breakpoint:
    console.print(f"Caching breakpoint function for this reproducer: {breakpoint}")
    cache[cache_key] = breakpoint
    _save_breakpoint_cache(cache_path, cache)

  if bug_type == "miscompilation":
    backtrace.pop()  # Pop out the topmost transformation function
    debugger.select_frame(
      backtrace[-1].func
    )  # Select the topmost frame for miscompilations

  return debugger, backtrace


def run_opt(
  rep: ReprodRes,
  *,
  harness: Harness,
  backtrace: StackTrace,
):
  # We get the transformation pass(es) and their bound analysis passes
  opt_passes = harness.llvmcode.resolve_pass_names(" ".join(rep.command))
  analy_passes = harness.llvmcode.resolve_related_passes(opt_passes)
  console.print(f"Transform passes: {', '.join(opt_passes) or '<none>'}")
  console.print(f"Analysis passes: {', '.join(analy_passes) or '<none>'}")

  # We run opt with the reproducer to collect verbose log
  opt_args = list(rep.command[1:])
  for p in opt_passes:
    opt_args += harness.llvmcode.resolve_pass_opts(p)
  for idx in range(len(opt_args)):
    if opt_args[idx].count("-passes="):
      opt_args[idx] = "--passes=" + ",".join(analy_passes + opt_passes)
  opt_args.remove(str(rep.file_path))
  for ap in analy_passes:
    opt_args += harness.llvmcode.resolve_pass_opts(ap)
  opt_args.append(
    "--debug-only="
    + ",".join(
      harness.llvmcode.resolve_debug_types(set([frame.file for frame in backtrace]))
    )
  )

  bug_type = harness.fixenv.get_bug_type()

  console.print("Running opt with the reproducer to collect verbose log ...")
  console.print(f"Options: {opt_args}")
  # TODO: `lli` leverages return code to indicate the success or failure, rather than the output.
  opt_cmd, opt_log = harness.run_opt(
    rep.file_path,
    opt_args,
    check=bug_type != "crash",
    # Run opt with the reproducer and useful options
    env={
      "LLVM_DISABLE_CRASH_REPORT": "1",
      "LLVM_DISABLE_SYMBOLIZATION": "1",
    },
  )
  if bug_type == "crash" and "PLEASE submit a bug report to " in opt_log:
    # Ignore the stack trace from the crash report
    opt_log = opt_log[: opt_log.find("PLEASE submit a bug report to ")]
  console.printb(title="Opt Verbose Log", message=f"$ {opt_cmd}\n{opt_log}")

  return ",".join(opt_passes), opt_cmd, opt_log


def _get_enabled_skills(
  harness: Harness, enabled: set[str]
) -> list[tuple[Path, int, Optional[int]] | tuple[Path, int, Optional[int], bool]]:
  """Get harness-provided skills filtered by the enabled set.

  Skills in ``DEFERRED_TOOLS`` are returned as 4-tuples ``(sk, budget, None, True)``;
  the agent uses tool_search to load full specs."""
  skills: list[
    tuple[Path, int, Optional[int]] | tuple[Path, int, Optional[int], bool]
  ] = []
  for sk in harness.get_skills():
    if sk.name not in enabled:
      continue
    if sk.name in DEFERRED_TOOLS:
      skills.append((sk, MAX_TCS_HEAVYWEIGHT_TOOLS, MAX_TCS_LIGHTWEIGHT_TOOLS, True))
    else:
      skills.append((sk, MAX_TCS_HEAVYWEIGHT_TOOLS, MAX_TCS_LIGHTWEIGHT_TOOLS))
  return skills


def _get_enabled_tools(
  harness: Harness, enabled: set[str]
) -> list[tuple[FuncToolBase, int] | tuple[FuncToolBase, int, bool]]:
  """Get harness-provided tools filtered by the enabled set.

  Tools in ``DEFERRED_TOOLS`` are returned as 3-tuples ``(tool, budget, True)``;
  the registry wraps them and auto-creates ``tool_search``.
  """
  tools: list[tuple[FuncToolBase, int] | tuple[FuncToolBase, int, bool]] = []
  for tool in harness.make_tools():
    name = tool.name()
    if name not in enabled:
      continue
    if name in HEAVYWEIGHT_TOOLS:
      budget = MAX_TCS_HEAVYWEIGHT_TOOLS
    elif name in LIGHTWEIGHT_TOOLS:
      budget = MAX_TCS_LIGHTWEIGHT_TOOLS
    else:
      panic(f"Tool {name} does not have a defined tool call limit.")
    if name in DEFERRED_TOOLS:
      tools.append((tool, budget, True))
    else:
      tools.append((tool, budget))
  return tools


def _create_reason_agent(
  agent_config: AgentConfig, harness: Harness, *, interactive: bool = False
) -> AgentBase:
  """Create a fresh agent with reason-stage tools and skills."""
  tools = _get_enabled_tools(harness, ENABLED_REASON_TOOLS)
  tools.append(
    (SubmitAnalysisTool(harness.acl, MIN_EDITPOINT_LINES), MAX_TCS_LIGHTWEIGHT_TOOLS)
  )
  if interactive:
    from harness.tools.askq import AskQuestionTool

    tools.append((AskQuestionTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  return agent_config.create_agent(
    tools=tools,
    skills=_get_enabled_skills(harness, ENABLED_REASON_SKILLS),
  )


def _create_repair_agent(
  agent_config: AgentConfig, harness: Harness, *, interactive: bool = False
) -> AgentBase:
  """Create a fresh agent with repair-stage tools and skills."""
  tools = _get_enabled_tools(harness, ENABLED_REPAIR_TOOLS)
  tools.append((SubmitPatchReportTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  tools.append((TodoTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  if interactive:
    from harness.tools.askq import AskQuestionTool

    tools.append((AskQuestionTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  agent = agent_config.create_agent(
    tools=tools,
    skills=_get_enabled_skills(harness, ENABLED_REPAIR_SKILLS),
  )
  agent.register_tool(SubAgentTool(agent), MAX_TCS_HEAVYWEIGHT_TOOLS)
  return agent


def _create_curate_insight_agent(
  agent_config: AgentConfig, harness: Harness
) -> AgentBase:
  """Create a fresh agent with curation-stage tools and skills."""
  tools = _get_enabled_tools(harness, ENABLED_CURATE_INSIGHT_TOOLS)
  return agent_config.create_agent(
    tools=tools,
    skills=_get_enabled_skills(harness, ENABLED_CURATE_INSIGHT_SKILLS),
  )


def curate_new_insights(
  *,
  aconf: AgentConfig,
  harness: Harness,
  pass_name: str,
  reproducer: str,
  patch: Optional[str],
  patch_report: Optional[str],
  run_outcome: str,
):
  try:
    harness.get_skill("llvm-insight-reflect")
  except KeyError:
    console.print(
      "WARNING: Skill `llvm-insight-reflect` not found, skip curating new insight.",
      color="yellow",
    )
    return  # Skill not installed

  agent = _create_curate_insight_agent(aconf, harness)
  summary = patch_report or "(no patch report available)"
  agent.append_user_message(
    f"Run the `llvm-insight-reflect` skill with these arguments:\n"
    f"- run_outcome: {run_outcome}\n"
    f"- pass_name: {pass_name}\n"
    f"- reproducer: {reproducer}\n"
    f"- patch: {patch or '(no patch)'}\n"
    f"- summary: {summary}\n"
  )

  def _response_cb(_: str) -> Tuple[bool, str]:
    return True, "Please call the `llvm-insight-reflect` skill."

  def _tool_cb(name: str, _: str, res: str) -> Tuple[bool, str]:
    if name == "llvm-insight-reflect":
      return False, res
    return True, res

  console.print("Running insight curation ...")
  try:
    result = agent.run(
      AgentHooks(post_response=_response_cb, post_tool_call=_tool_cb),
    )
    console.print(f"Curation complete: {result[:200] if result else '(no output)'}")
  except Exception as e:
    console.print(f"WARNING: Curation failed (non-fatal): {e}", color="yellow")


def autofix(
  rep: ReprodRes,
  *,
  harness: Harness,
  aconf: AgentConfig,
  stats: RunStats,
  interactive: bool = False,
):
  # We use a debugger to help the agent understand the context
  debugger, backtrace = prepare_debugger(rep, harness=harness)
  stats.trans_point = backtrace[-1].as_tuple()

  # For ad-hoc reproducers (no bench-curated lit_test_dir), narrow the lit
  # regression scope to the dirs implicated by the bug. If we can't narrow
  # it, fall back to the full middle-end suite — slower but safe.
  configure_lit_test_dirs(
    harness,
    rep.raw_command,
    backtrace=backtrace,
    log=console.print,
    warn=lambda msg: console.print(msg, color="yellow"),
  )

  # Run opt to get the optimization pass and the verbose log of the reproducer's execution
  # These information will help the agent understand the context better
  opt_pass, opt_cmd, opt_log = run_opt(
    rep, harness=harness, backtrace=backtrace.clone()
  )
  stats.opt_pass = opt_pass

  # Run the agent with all required information and tools
  return run_mini_agent(
    rep,
    opt_pass=opt_pass,
    opt_cmd=opt_cmd,
    opt_log=opt_log,
    debugger=debugger,
    backtrace=backtrace,
    aconf=aconf,
    harness=harness,
    stats=stats,
    interactive=interactive,
  )


def log_issue_banner(harness: Harness, args, log=print) -> None:
  """Emit a one-shot banner describing the input source.

  Bench (``--issue``): issue id, title, labels, base commit.
  Ad-hoc (``--reproducer`` or ``--autoreduce``): reproducer file, command,
  bug type, base commit. Shared across mini/xcli/mswe so the surface stays
  uniform.
  """
  if args.issue is not None and not getattr(args, "autoreduce", False):
    log(f"Issue ID: {args.issue}")
    log(f"Issue Type: {harness.fixenv.get_bug_type()}")
    log(f"Issue Commit: {harness.fixenv.get_base_commit()}")
    log(f"Issue Title: {harness.fixenv.get_issue_title()}")
    log(f"Issue Labels: {harness.fixenv.get_issue_labels()}")
  else:
    repro = harness.fixenv.card.reproducers[0]
    log(f"Reproducer File: {repro.file}")
    log(f"Reproducer Command: {repro.commands[0]}")
    log(f"Bug Type: {harness.fixenv.get_bug_type()}")
    log(f"Base Commit: {harness.fixenv.get_base_commit()}")


def add_common_args(parser: ArgumentParser) -> None:
  """Register ``--issue``/``--reproducer`` (mutex), ``--base-commit``, and
  ``--pull-latest``.

  Shared across ``autofix.mini``, ``autofix.xcli``, and ``autofix.mswe`` so
  every entry point exposes the same input surface. ``--pull-latest`` and
  ``--base-commit`` compose: ``--pull-latest`` runs first (``git pull
  origin main``), then ``--base-commit``'s checkout — useful when the
  target commit was only just published.
  """
  src = parser.add_mutually_exclusive_group(required=True)
  src.add_argument(
    # TODO: use --issue in the future for github issue ID
    "--issue",
    type=str,
    default=None,
    help="The bench issue ID to fix.",
  )
  src.add_argument(
    "--reproducer",
    type=str,
    default=None,
    help=(
      "Path to an ad-hoc reproducer .ll file. The file must embed two "
      "directives: `; BUG: crash|miscompilation` and `; RUN: opt ...`."
    ),
  )
  parser.add_argument(
    "--base-commit",
    type=str,
    default=None,
    help=(
      "Git commit to base the fix on (overrides bench-provided base commit). "
      "Defaults to HEAD of the already pulled LLVM source tree."
    ),
  )
  parser.add_argument(
    "--pull-latest",
    action="store_true",
    default=False,
    help=(
      "Pull the latest LLVM source tree before building the harness. "
      "Composes with --base-commit (pull first, then checkout)."
    ),
  )
  parser.add_argument(
    "--autoreduce",
    action="store_true",
    default=False,
    help=(
      "Treat --issue as a GitHub issue ID in dtcxzyw/llvm-autoreduce. "
      "The issue is fetched and normalized by the model into an "
      "ad-hoc reproducer, then handled as if --reproducer had been used. "
      "Implies --pull-latest. "
      "Requires --issue."
    ),
  )


def build_harness_from_args(
  args, *, agent_config: AgentConfig, aggressive_testing: bool, do_print: Callable
) -> Harness:
  """Construct a :class:`Harness` from CLI args produced by
  :func:`add_common_args`. Raises ``ValueError`` on a malformed reproducer file.
  """
  if args.pull_latest or args.autoreduce:
    from harness.llvm.intern import llvm as llvm_ops

    llvm_ops.pull_latest()

  if args.autoreduce:
    if args.issue is None:
      raise ValueError("--autoreduce requires --issue <github-issue-id>")
    from autofix.autored import fetch_autoreduce_reproducer

    do_print(f"Fetching autoreduce reproducer for issue {args.issue} ...")
    rpath, rinfo = fetch_autoreduce_reproducer(args.issue, agent_config=agent_config)
    do_print(f"Autoreduced issue {args.issue} -> {rpath}")
    return Harness.from_reproducer(
      str(rpath),
      base_commit=rinfo.llvm_commit,
      cmake_args=ADDITIONAL_CMAKE_FLAGS,
      aggressive_testing=aggressive_testing,
    )

  if args.issue is not None:
    return Harness.from_issue_id(
      args.issue,
      base_commit=args.base_commit,
      cmake_args=ADDITIONAL_CMAKE_FLAGS,
      aggressive_testing=aggressive_testing,
    )
  return Harness.from_reproducer(
    args.reproducer,
    base_commit=args.base_commit,
    cmake_args=ADDITIONAL_CMAKE_FLAGS,
    aggressive_testing=aggressive_testing,
  )


def build_agent_config(driver: str, model: str, debug: bool) -> AgentConfig:
  # Set up used LLMs and agents
  if driver == "openai":
    from harness.lms.openai_generic import GPTGenericAgent

    driver_class = GPTGenericAgent
  elif driver == "anthropic":
    from harness.lms.anthropic_generic import ClaudeGenericAgent

    driver_class = ClaudeGenericAgent
  else:
    panic(f"Unsupported LLM API driver: {driver}")

  return AgentConfig(
    driver_class=driver_class,
    model=model,
    temperature=AGENT_TEMPERATURE,
    top_p=AGENT_TOP_P,
    max_completion_tokens=AGENT_MAX_COMPLETION_TOKENS,
    reasoning_effort=AGENT_REASONINT_EFFORT,
    debug_mode=debug,
  )


def parse_args():
  parser = ArgumentParser(description="llvm-autofix (mini)")
  add_common_args(parser)
  parser.add_argument(
    "--model",
    type=str,
    required=True,
    help="The LLM model to use for the agent.",
  )
  parser.add_argument(
    "--driver",
    type=str,
    default="openai",
    help="The LLM API driver to use (default: openai).",
    choices=["openai", "anthropic"],
  )
  parser.add_argument(
    "--stats",
    type=str,
    default=None,
    help="Path to save the generation statistics as a JSON file (default: None).",
  )
  parser.add_argument(
    "--debug",
    action="store_true",
    default=False,
    help="Enable debug mode for more verbose output (default: False).",
  )
  parser.add_argument(
    "--aggressive-testing",
    action="store_true",
    default=False,
    help="Use all Transforms and Analysis tests for testing patches online (default: False).",
  )
  parser.add_argument(
    "--interactive",
    action="store_true",
    default=False,
    help="Enable the ask tool so the agent can ask the user questions (default: False).",
  )
  return parser.parse_args()


def main():
  harness.require_home_dir()

  args = parse_args()

  # Set up the console for output
  if args.debug:
    global console
    console = get_boxed_console(debug_mode=True)

  aconf = build_agent_config(args.driver, args.model, args.debug)
  GlobalMeter.configure(
    token_limit=AGENT_MAX_CONSUMED_TOKENS,
    round_limit=AGENT_MAX_CHAT_ROUNDS,
  )

  # Set up saved statistics and output
  stats_path = None
  if args.stats:
    stats_path = Path(args.stats)
    if stats_path.exists():
      panic(f"Stats file {stats_path} already exists.")

  # --- Use Harness for LLVM setup and reproduction ---
  try:
    harness_ctx = build_harness_from_args(
      args,
      agent_config=aconf,
      aggressive_testing=args.aggressive_testing,
      do_print=console.print,
    )
  except ValueError as e:
    panic(str(e))

  with harness_ctx as h:
    bug_type = h.fixenv.get_bug_type()
    if bug_type not in ["crash", "miscompilation"]:
      panic(f"Unsupported bug type: {bug_type}")

    log_issue_banner(h, args, log=console.print)

    console.print("Building LLVM and try reproducing the issue ...")
    rep = h.reproduce()
    console.print("Issue reproduced successfully.")
    console.printb(title="Reproducer", message=rep.source)
    console.printb(
      title="Reproducing Log", message=f"$ {rep.raw_command}\n{rep.symptom}"
    )

    # Start analyzing and repairing the issue
    stats = RunStats(command=vars(args))
    stats.total_time_sec = time.time()
    try:
      stats.patch = autofix(
        rep,
        harness=h,
        aconf=aconf,
        stats=stats,
        interactive=args.interactive,
      )
      if not stats.patch:
        raise NoAvailablePatchFound("All efforts tried yet no available patches found.")
      # Post validation when necessary
      if not h.fixenv.use_entire_regression_test_suite:
        console.print("Post-validating the generated patch ...")
        passed, errmsg = h.post_validate()
        if not passed:
          stats.patch = None
          console.printb(title="Post-validation", message=errmsg)
          raise NoAvailablePatchFound("Post validation failed")
        console.print("Passed")
      # Run insight curation after successful fix
      curate_new_insights(
        aconf=aconf,
        harness=h,
        pass_name=stats.opt_pass.lower().replace(" ", "-"),
        reproducer=rep.source,
        patch=stats.patch,
        patch_report=stats.patch_report,
        run_outcome="success",
      )
    except Exception as e:
      import traceback

      stats.error = type(e).__name__
      stats.errmsg = str(e)
      stats.traceback = traceback.format_exc()

      raise e
    finally:
      gm = GlobalMeter.instance()
      stats.chat_rounds = gm.total_rounds
      stats.input_tokens = gm.total_input_tokens
      stats.output_tokens = gm.total_output_tokens
      stats.cached_tokens = gm.total_cached_tokens
      stats.total_tokens = gm.total_tokens
      stats.total_time_sec = time.time() - stats.total_time_sec
      if stats_path:
        with stats_path.open("w") as fout:
          json.dump(stats.as_dict(), fout, indent=2)
        console.print(f"Generation statistics saved to {stats_path}.")

    console.print("Final Patch")
    console.print("-----------")
    console.print(stats.patch)
    console.print("Reference Patch")
    console.print("---------------")
    console.print(h.fixenv.get_reference_patch())
    console.print("Statistics")
    console.print("----------")
    console.print(json.dumps(stats.as_dict(), indent=2))


if __name__ == "__main__":
  main()
