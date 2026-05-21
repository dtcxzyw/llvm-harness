import json
import time
import traceback
from argparse import ArgumentParser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import harness
from autoreview.pr_extract import PRInfo, fetch_pr_info
from harness.llvm import Harness
from harness.lms.agent import AgentBase, AgentConfig, AgentHooks
from harness.lms.meter import GlobalMeter
from harness.lms.tool import (
  FuncToolBase,
  FuncToolCallException,
  FuncToolSpec,
  StatefulFuncToolBase,
  StatelessFuncToolBase,
)
from harness.tools.subagent import SubAgentTool
from harness.tools.todo import TodoTool
from harness.utils.console import get_boxed_console

_PROMPTS = harness.load_yaml_config("autoreview", "archer.yaml")["prompts"]
PROMPT_SYSTEM = _PROMPTS["system"]
PROMPT_ANALYZE = _PROMPTS["analyze"]
PROMPT_REVIEW = _PROMPTS["review"]

AGENT_TEMPERATURE = 0
AGENT_TOP_P = 0.95
AGENT_MAX_COMPLETION_TOKENS = 8192
AGENT_REASONING_EFFORT = "medium"
AGENT_MAX_CHAT_ROUNDS = 500
AGENT_MAX_CONSUMED_TOKENS = 5_000_000

MAX_TCS_LIGHTWEIGHT_TOOLS = 250
MAX_TCS_HEAVYWEIGHT_TOOLS = 25

ENABLED_ANALYZE_TOOLS = {
  "list",
  "read",
  "find",
  "grep",
  "ripgrep",
  "llvm_langref",
  "insight",
  "subagent",
}
ENABLED_REVIEW_TOOLS = {
  "list",
  "read",
  "find",
  "grep",
  "ripgrep",
  "write",
  "edit",
  "llvm_langref",
  "llvm_optimize_ir",
  "llvm_verify_optim",
  "llvm_check_optim",
  "llvm_verify_ir",
  "llvm_execute_ir",
  "llvm_interpret_ir",
  "insight",
  "subagent",
  "todo",
}

ENABLED_ANALYZE_SKILLS = {"llvm-insight-search"}
ENABLED_REVIEW_SKILLS = {"llvm-insight-search"}
ENABLED_CURATE_INSIGHT_TOOLS = {"read", "ripgrep", "insight"}
ENABLED_CURATE_INSIGHT_SKILLS = {"llvm-insight-reflect"}

ALL_ENABLED_TOOLS = (
  ENABLED_ANALYZE_TOOLS | ENABLED_REVIEW_TOOLS | ENABLED_CURATE_INSIGHT_TOOLS
)
HEAVYWEIGHT_TOOLS = {"subagent", "llvm_verify_optim", "llvm_check_optim"}
LIGHTWEIGHT_TOOLS = ALL_ENABLED_TOOLS - HEAVYWEIGHT_TOOLS
DEFERRED_TOOLS = {
  "insight",
  "llvm_langref",
  "llvm_optimize_ir",
  "llvm_verify_optim",
  "llvm_check_optim",
  "llvm_verify_ir",
  "llvm_execute_ir",
  "llvm_interpret_ir",
  "llvm-insight-search",
  "llvm-insight-reflect",
}
VERIFICATION_TOOLS = {
  "llvm_verify_optim",
  "llvm_check_optim",
  "llvm_verify_ir",
  "llvm_execute_ir",
  "llvm_interpret_ir",
}

CRASH_INDICATORS = {
  "LLVM ERROR",
  "compilation aborted",
  "Stack dump:",
  "Broken module found",
  "does not dominate all uses",
  "PLEASE submit a bug report",
  "opt crashed:",
}

CRASH_FALSE_POSITIVES = {
  "PHI nodes not grouped at top of basic block!",
  "immarg operand has non-immediate parameter",
  "fpmath requires a floating point result!",
  "did not reach a fixpoint",
}

console = get_boxed_console(debug_mode=False)


@dataclass
class ReviewBug:
  tool: str
  log: str
  original_ir: str = "<unavailable>"
  transformed_ir: str = "<unavailable>"


@dataclass
class RunStats:
  command: dict
  error: Optional[str] = None
  errmsg: Optional[str] = None
  traceback: Optional[str] = None
  input_tokens: int = 0
  output_tokens: int = 0
  cached_tokens: int = 0
  total_tokens: int = 0
  chat_rounds: int = 0
  total_time_sec: float = 0.0
  analyze_rounds: int = 0
  review_rounds: int = 0
  strategies: list[dict] = field(default_factory=list)
  analyze_thoughts: str = ""
  review_report: Optional[str] = None
  review_traj: list[str] = field(default_factory=list)
  bugs: list[ReviewBug] = field(default_factory=list)

  def as_dict(self) -> dict:
    return asdict(self)


def panic(msg: str):
  console.print(f"Error: {msg}", color="red")
  exit(1)


class ReachToolBudget(Exception):
  pass


@dataclass
class Test:
  test_name: str
  test_body: str
  commands: list[str] = field(default_factory=list)
  tested: bool = False
  covered_strategies: set[str] = field(default_factory=set)


class SubmitAnalysisTool(StatelessFuncToolBase):
  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "submit_analysis",
      "Stop analysis and submit phase-1 strategy results.",
      [
        FuncToolSpec.Param(
          "strategies",
          "list[dict]",
          True,
          "A list of strategy objects with name, target, rationale, expected_issue.",
        ),
        FuncToolSpec.Param(
          "thoughts",
          "string",
          True,
          "Detailed analysis reasoning for the PR.",
        ),
      ],
      [],
    )

  def _call(self, *, strategies, thoughts: str, **kwargs) -> str:
    if isinstance(strategies, str):
      try:
        strategies = json.loads(strategies)
      except Exception as e:
        raise FuncToolCallException("strategies must be a JSON array.") from e

    if not isinstance(strategies, list):
      raise FuncToolCallException("strategies must be a list.")

    normalized = []
    required = ["name", "target", "rationale", "expected_issue"]
    for idx, strategy in enumerate(strategies):
      if not isinstance(strategy, dict):
        raise FuncToolCallException(f"strategies[{idx}] must be an object.")
      missing = [k for k in required if k not in strategy]
      if missing:
        raise FuncToolCallException(
          f"strategies[{idx}] missing fields: {', '.join(missing)}"
        )
      normalized.append(
        {
          "name": str(strategy["name"]).strip(),
          "target": str(strategy["target"]).strip(),
          "rationale": str(strategy["rationale"]).strip(),
          "expected_issue": str(strategy["expected_issue"]).strip(),
        }
      )

    return json.dumps({"strategies": normalized, "thoughts": thoughts}, indent=2)


class SubmitReviewReportTool(StatelessFuncToolBase):
  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "submit_reviewreport",
      "Submit the final review report after phase-2 validation.",
      [
        FuncToolSpec.Param(
          "report",
          "string",
          True,
          "Final markdown review report.",
        ),
        FuncToolSpec.Param(
          "force",
          "boolean",
          False,
          "Set true to stop early before all extracted tests are marked tested.",
        ),
      ],
      [],
    )

  def _call(self, *, report: str, force: bool = False, **kwargs) -> str:
    if not report or not report.strip():
      raise FuncToolCallException("report must not be empty.")
    return json.dumps({"report": report.strip(), "force": bool(force)}, indent=2)


class TestsTool(StatefulFuncToolBase):
  def __init__(
    self,
    tests: list[Test],
    strategies: list[dict] | None = None,
    validator=None,
  ):
    self.tests = tests
    self.strategies = strategies or []
    self.all_strategies = {
      str(strategy.get("name")).strip()
      for strategy in self.strategies
      if str(strategy.get("name", "")).strip()
    }
    self.validator = validator

  def fresh(self) -> "TestsTool":
    cloned = [
      Test(
        test_name=test.test_name,
        test_body=test.test_body,
        commands=list(test.commands),
        tested=test.tested,
        covered_strategies=set(test.covered_strategies),
      )
      for test in self.tests
    ]
    return TestsTool(cloned, strategies=list(self.strategies), validator=self.validator)

  def get_uncovered_strategies(self, index: int) -> list[str]:
    if index < 0 or index >= len(self.tests):
      return []
    return sorted(self.all_strategies - self.tests[index].covered_strategies)

  def get_all_uncovered_strategies(self) -> dict[int, list[str]]:
    uncovered = {}
    for i, _ in enumerate(self.tests):
      remaining = self.get_uncovered_strategies(i)
      if remaining:
        uncovered[i] = remaining
    return uncovered

  def all_tests_tested(self) -> bool:
    return all(test.tested for test in self.tests)

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "tests_manager",
      "Manage extracted PR tests, strategy coverage, and verification progress.",
      [
        FuncToolSpec.Param(
          "action",
          "string",
          True,
          "One of: list, get, confirm_strategy, mark_tested.",
        ),
        FuncToolSpec.Param(
          "index",
          "integer",
          False,
          "Required for get, confirm_strategy, and mark_tested.",
        ),
        FuncToolSpec.Param(
          "strategy",
          "string",
          False,
          "Required for confirm_strategy. Must match a Phase-1 strategy name.",
        ),
      ],
      [],
    )

  def _call(
    self,
    *,
    action: str,
    index: Optional[int] = None,
    strategy: Optional[str] = None,
    **kwargs,
  ) -> str:
    if action == "list":
      all_tested = self.all_tests_tested()
      uncovered = self.get_all_uncovered_strategies()
      return json.dumps(
        {
          "tests": [
            {
              "index": i,
              "name": t.test_name,
              "tested": t.tested,
              "commands": t.commands,
              "covered_strategies": sorted(t.covered_strategies),
              "uncovered_strategies": self.get_uncovered_strategies(i),
            }
            for i, t in enumerate(self.tests)
          ],
          "all_tested": all_tested,
          "all_strategies_covered": not uncovered,
          "message": (
            "All tests have been tested and every test covers all Phase-1 strategies."
            if all_tested and not uncovered
            else "Some tests still need verification or strategy coverage."
          ),
        },
        indent=2,
      )

    if index is None:
      raise FuncToolCallException("index is required for this action.")
    if index < 0 or index >= len(self.tests):
      raise FuncToolCallException(
        f"Invalid test index {index}. Valid range: 0..{len(self.tests) - 1}."
      )

    selected = self.tests[index]
    if action == "get":
      return json.dumps(
        {
          "test_name": selected.test_name,
          "test_body": selected.test_body,
          "commands": selected.commands,
          "tested": selected.tested,
          "covered_strategies": sorted(selected.covered_strategies),
          "uncovered_strategies": self.get_uncovered_strategies(index),
        },
        indent=2,
      )

    if action == "confirm_strategy":
      if strategy is None or not strategy.strip():
        raise FuncToolCallException("strategy is required for confirm_strategy.")

      strategy_name = strategy.strip()
      if strategy_name not in self.all_strategies:
        raise FuncToolCallException(
          "Unknown strategy "
          f"'{strategy_name}'. Valid strategies: {sorted(self.all_strategies)}"
        )

      if strategy_name in selected.covered_strategies:
        return (
          f"Strategy '{strategy_name}' was already confirmed for test {index}. "
          f"Remaining uncovered strategies: {self.get_uncovered_strategies(index)}"
        )

      if self.validator is not None:
        ok, reason = self.validator(action=action, index=index, strategy=strategy_name)
        if not ok:
          return (
            f"Strategy '{strategy_name}' NOT confirmed for test {index}. "
            f"Reason: {reason}"
          )

      selected.covered_strategies.add(strategy_name)
      uncovered = self.get_uncovered_strategies(index)
      if not uncovered:
        return (
          f"Strategy '{strategy_name}' confirmed for test {index}. "
          "This test now covers all Phase-1 strategies."
        )
      return (
        f"Strategy '{strategy_name}' confirmed for test {index}. "
        f"Remaining uncovered strategies: {uncovered}"
      )

    if action == "mark_tested":
      if self.validator is not None:
        ok, reason = self.validator(action=action, index=index)
        if not ok:
          return f"Test {index} NOT marked as tested. Reason: {reason}"

      uncovered = self.get_uncovered_strategies(index)
      if uncovered:
        return (
          f"Test {index} NOT marked as tested. Uncovered strategies remain: {uncovered}. "
          "Confirm each strategy after corresponding verification before marking tested."
        )

      selected.tested = True
      all_tested = all(t.tested for t in self.tests)
      all_uncovered = self.get_all_uncovered_strategies()
      if all_tested and not all_uncovered:
        return (
          f"Test {index} marked as tested. All extracted tests are covered and all "
          "Phase-1 strategies have been confirmed for every test."
        )
      return f"Test {index} marked as tested. Continue with remaining tests."

    raise FuncToolCallException(f"Invalid action '{action}'.")


def ensure_tools_available(agent: AgentBase, tools: list[str]):
  available_tools = agent.tools.list(ignore_budget=False)
  unavailable = [tool for tool in tools if tool not in available_tools]
  if unavailable:
    raise ReachToolBudget(f"Tools [{', '.join(unavailable)}] are out of budget.")


def _get_enabled_skills(
  h: Harness, enabled: set[str]
) -> list[tuple[Path, int, Optional[int]] | tuple[Path, int, Optional[int], bool]]:
  skills: list[
    tuple[Path, int, Optional[int]] | tuple[Path, int, Optional[int], bool]
  ] = []
  for skill in h.get_skills():
    if skill.name not in enabled:
      continue
    if skill.name in DEFERRED_TOOLS:
      skills.append((skill, MAX_TCS_HEAVYWEIGHT_TOOLS, MAX_TCS_LIGHTWEIGHT_TOOLS, True))
    else:
      skills.append((skill, MAX_TCS_HEAVYWEIGHT_TOOLS, MAX_TCS_LIGHTWEIGHT_TOOLS))
  return skills


def _get_enabled_tools(
  h: Harness, enabled: set[str]
) -> list[tuple[FuncToolBase, int] | tuple[FuncToolBase, int, bool]]:
  tools: list[tuple[FuncToolBase, int] | tuple[FuncToolBase, int, bool]] = []
  for tool in h.make_tools():
    name = tool.name()
    if name not in enabled:
      continue
    if name in HEAVYWEIGHT_TOOLS:
      budget = MAX_TCS_HEAVYWEIGHT_TOOLS
    elif name in LIGHTWEIGHT_TOOLS:
      budget = MAX_TCS_LIGHTWEIGHT_TOOLS
    else:
      panic(f"Tool {name} does not have a defined tool call budget.")
    if name in DEFERRED_TOOLS:
      tools.append((tool, budget, True))
    else:
      tools.append((tool, budget))
  return tools


def _create_analyze_agent(
  aconf: AgentConfig, h: Harness, *, interactive: bool = False
) -> AgentBase:
  tools = _get_enabled_tools(h, ENABLED_ANALYZE_TOOLS)
  tools.append((SubmitAnalysisTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  if interactive:
    from harness.tools.askq import AskQuestionTool

    tools.append((AskQuestionTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  agent = aconf.create_agent(
    tools=tools,
    skills=_get_enabled_skills(h, ENABLED_ANALYZE_SKILLS),
  )
  agent.register_tool(SubAgentTool(agent), MAX_TCS_HEAVYWEIGHT_TOOLS)
  return agent


def _create_review_agent(
  aconf: AgentConfig, h: Harness, *, interactive: bool = False
) -> AgentBase:
  tools = _get_enabled_tools(h, ENABLED_REVIEW_TOOLS)
  tools.append((SubmitReviewReportTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  tools.append((TodoTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  if interactive:
    from harness.tools.askq import AskQuestionTool

    tools.append((AskQuestionTool(), MAX_TCS_LIGHTWEIGHT_TOOLS))
  agent = aconf.create_agent(
    tools=tools,
    skills=_get_enabled_skills(h, ENABLED_REVIEW_SKILLS),
  )
  agent.register_tool(SubAgentTool(agent), MAX_TCS_HEAVYWEIGHT_TOOLS)
  return agent


def _create_curate_insight_agent(aconf: AgentConfig, h: Harness) -> AgentBase:
  tools = _get_enabled_tools(h, ENABLED_CURATE_INSIGHT_TOOLS)
  return aconf.create_agent(
    tools=tools,
    skills=_get_enabled_skills(h, ENABLED_CURATE_INSIGHT_SKILLS),
  )


def curate_new_insights(
  *,
  aconf: AgentConfig,
  h: Harness,
  pr_info: PRInfo,
  stats: RunStats,
  run_outcome: str,
):
  try:
    h.get_skill("llvm-insight-reflect")
  except KeyError:
    console.print(
      "WARNING: Skill `llvm-insight-reflect` not found, skip curation.",
      color="yellow",
    )
    return

  agent = _create_curate_insight_agent(aconf, h)
  summary = stats.review_report or "(no review report available)"
  agent.append_user_message(
    f"Run the `llvm-insight-reflect` skill with these arguments:\n"
    f"- run_outcome: {run_outcome}\n"
    f"- pass_name: {','.join(pr_info.components) or 'unknown'}\n"
    f"- reproducer: {json.dumps(pr_info.tests[:1], ensure_ascii=False)}\n"
    f"- patch: {pr_info.patch[:5000]}\n"
    f"- summary: {summary}\n"
  )

  def response_cb(_: str):
    return True, "Please call the `llvm-insight-reflect` skill."

  def tool_cb(name: str, _: str, res: str):
    if name == "llvm-insight-reflect":
      return False, res
    return True, res

  try:
    console.print("Running insight curation ...")
    agent.run(AgentHooks(post_response=response_cb, post_tool_call=tool_cb))
  except Exception as e:
    console.print(f"WARNING: Curation failed (non-fatal): {e}", color="yellow")


def _build_tests_overview(pr_info: PRInfo) -> str:
  rows = []
  index = 0
  for test_file in pr_info.tests:
    source = test_file.get("file", "<unknown>")
    for test in test_file.get("tests", []):
      rows.append(f"- [{index}] {source} :: {test.get('test_name', '<unnamed>')}")
      index += 1
  if not rows:
    return "<no tests extracted>"
  return "\n".join(rows)


def get_component_knowledge(components: list[str]) -> str:
  knowledge_dir = Path(harness.require_home_dir()) / "insight" / "shared"
  if not knowledge_dir.exists():
    return "No specific knowledge provided for these components."
  chunks = []
  for component in components:
    candidate = knowledge_dir / f"{component}.md"
    if candidate.exists():
      chunks.append(candidate.read_text(encoding="utf-8"))
  return (
    "\n".join(chunks)
    if chunks
    else "No specific knowledge provided for these components."
  )


def _flatten_tests(pr_info: PRInfo) -> list[Test]:
  tests: list[Test] = []
  for test_file in pr_info.tests:
    commands = test_file.get("commands", [])
    for test in test_file.get("tests", []):
      tests.append(
        Test(
          test_name=test.get("test_name", "<unnamed>"),
          test_body=test.get("test_body", ""),
          commands=commands,
        )
      )
  return tests


def _read_if_exists(path_str: str) -> str:
  try:
    path = Path(path_str)
    if path.exists() and path.is_file():
      return path.read_text(encoding="utf-8")
  except Exception:
    pass
  return "<unavailable>"


def _is_opt_crash_report(log: str) -> bool:
  if any(marker in log for marker in CRASH_FALSE_POSITIVES):
    return False
  return any(marker in log for marker in CRASH_INDICATORS)


def _collect_bug_if_any(stats: RunStats, name: str, args_json: str, res: str):
  found = False
  if name in {"llvm_verify_optim", "llvm_verify_ir"} and res.startswith(
    "Transformation is INCORRECT"
  ):
    found = True
  if name == "llvm_check_optim" and res.startswith("Optimization is INCORRECT"):
    found = True
  if name in VERIFICATION_TOOLS and _is_opt_crash_report(res):
    found = True
  if not found:
    return

  try:
    args = json.loads(args_json)
  except Exception:
    args = {}

  if name == "llvm_verify_ir":
    before = _read_if_exists(args.get("src_path", ""))
    after = _read_if_exists(args.get("tgt_path", ""))
  else:
    before = _read_if_exists(args.get("input_path", ""))
    after = "<generated internally by llvm tool>"

  stats.bugs.append(
    ReviewBug(tool=name, log=res, original_ir=before, transformed_ir=after)
  )


def setup_llvm_environment(pr_info: PRInfo, h: Harness):
  from harness.llvm.intern import llvm as llvm_ops

  console.print(f"Checking out base commit {pr_info.base_commit} ...")
  try:
    llvm_ops.reset(pr_info.base_commit)
  except Exception:
    console.print("Failed reset; pulling latest and retrying ...", color="yellow")
    llvm_ops.pull_latest()
    llvm_ops.reset(pr_info.base_commit)

  console.print("Applying PR patch ...")
  ok, log = h.apply_patch(pr_info.patch)
  if not ok:
    panic(f"Failed to apply patch:\n{log}")

  console.print("Building LLVM at patched revision ...")
  ok, log = h.build()
  if not ok:
    panic(f"Failed to build LLVM for review:\n{log}")


def run_archer_agent(
  pr_info: PRInfo,
  *,
  aconf: AgentConfig,
  h: Harness,
  stats: RunStats,
  interactive: bool = False,
) -> tuple[str, list[dict]]:
  tests = _flatten_tests(pr_info)
  tests_overview = _build_tests_overview(pr_info)

  analyze_agent = _create_analyze_agent(aconf, h, interactive=interactive)
  analyze_agent.append_system_message(PROMPT_SYSTEM)
  analyze_agent.append_user_message(
    PROMPT_ANALYZE.format(
      pr_id=pr_info.pr_id,
      title=pr_info.title,
      description=pr_info.description or "<no description>",
      components=", ".join(pr_info.components) or "<unknown>",
      patch=pr_info.patch,
      tests_overview=tests_overview,
      knowledge=get_component_knowledge(pr_info.components),
    )
  )

  def analyze_post_response(_: str):
    ensure_tools_available(analyze_agent, ["submit_analysis"])
    return (
      True,
      "Please continue with tool calls only and finish phase 1 with submit_analysis.",
    )

  def analyze_post_tool_call(name: str, _: str, res: str):
    ensure_tools_available(analyze_agent, ["submit_analysis"])
    if name != "submit_analysis":
      return True, res
    try:
      json.loads(res)
    except Exception:
      return True, res
    return False, res

  console.print("Phase 1: analyzing PR ...")
  analysis = analyze_agent.run(
    AgentHooks(
      post_response=analyze_post_response,
      post_tool_call=analyze_post_tool_call,
    )
  )
  stats.analyze_rounds = analyze_agent.meter.chat_rounds

  try:
    parsed = json.loads(analysis)
  except Exception as exc:
    analysis_preview = analysis[:200] if isinstance(analysis, str) else repr(analysis)
    raise RuntimeError(
      "Phase 1 did not return valid JSON from submit_analysis. "
      f"Received: {analysis_preview!r}"
    ) from exc
  if not isinstance(parsed, dict):
    raise RuntimeError(
      "Phase 1 submit_analysis payload must be a JSON object. "
      f"Received {type(parsed).__name__}."
    )
  stats.analyze_thoughts = parsed.get("thoughts", "")
  stats.strategies = list(parsed.get("strategies", []))

  review_agent = _create_review_agent(aconf, h, interactive=interactive)
  review_agent.append_system_message(PROMPT_SYSTEM)

  test_get_timestamps: dict[int, int] = {}
  verification_events: list[str] = []
  test_strategy_cursors: dict[int, int] = {}

  all_strategy_names = {
    str(strategy.get("name")).strip()
    for strategy in stats.strategies
    if str(strategy.get("name", "")).strip()
  }

  def validator(*, action: str, index: int, strategy: Optional[str] = None):
    if index not in test_get_timestamps:
      target = (
        f"confirming strategy '{strategy}'"
        if action == "confirm_strategy" and strategy is not None
        else "marking tested"
      )
      return (
        False,
        f"You must call tests_manager(action='get', index={index}) before {target}.",
      )

    if action == "confirm_strategy":
      if strategy is None or strategy not in all_strategy_names:
        return False, f"Unknown Phase-1 strategy: {strategy}"
      start = max(test_get_timestamps[index], test_strategy_cursors.get(index, 0))
      has_verify = any(
        name in VERIFICATION_TOOLS for name in verification_events[start:]
      )
      if not has_verify:
        return (
          False,
          "You must call at least one verification/execution tool after get and before confirming a strategy.",
        )
      test_strategy_cursors[index] = len(verification_events)
      return True, ""

    start = test_get_timestamps[index]
    has_verify = any(name in VERIFICATION_TOOLS for name in verification_events[start:])
    if not has_verify:
      return (
        False,
        "You must call at least one verification/execution tool after get and before mark_tested.",
      )
    return True, ""

  tests_manager = TestsTool(tests, strategies=stats.strategies, validator=validator)
  review_agent.register_tool(tests_manager, MAX_TCS_LIGHTWEIGHT_TOOLS)
  review_prompt = PROMPT_REVIEW.replace(
    "{strategies}",
    json.dumps(stats.strategies, ensure_ascii=False, indent=2),
  ).replace("{tests_overview}", tests_overview)
  review_agent.append_user_message(review_prompt)

  def review_post_response(_: str):
    ensure_tools_available(review_agent, ["submit_reviewreport", "tests_manager"])
    return (
      True,
      "Please continue with tool calls and finish by calling submit_reviewreport.",
    )

  def review_post_tool_call(name: str, args_json: str, res: str):
    ensure_tools_available(review_agent, ["submit_reviewreport", "tests_manager"])

    if name == "tests_manager":
      try:
        args_obj = json.loads(args_json)
        if args_obj.get("action") == "get" and args_obj.get("index") is not None:
          index = int(args_obj["index"])
          test_get_timestamps[index] = len(verification_events)
          test_strategy_cursors[index] = len(verification_events)
      except Exception:
        pass
      return True, res

    if name in VERIFICATION_TOOLS:
      verification_events.append(name)
      stats.review_traj.append(json.dumps({"tool": name, "result": res}))
      _collect_bug_if_any(stats, name, args_json, res)
      return True, res

    if name == "submit_reviewreport":
      payload = json.loads(res)
      force = bool(payload.get("force", False))
      all_tested = tests_manager.all_tests_tested()
      uncovered = tests_manager.get_all_uncovered_strategies()
      if (not all_tested or uncovered) and not force:
        reasons = []
        if not all_tested:
          reasons.append("some extracted tests are untested")
        if uncovered:
          reasons.append(f"strategy coverage is incomplete: {uncovered}")
        return (
          True,
          "Cannot submit yet: "
          + "; ".join(reasons)
          + ". Continue or use force=true with clear justification.",
        )
      stats.review_report = payload.get("report", "")
      return False, stats.review_report

    return True, res

  console.print("Phase 2: generating and validating review tests ...")
  result = review_agent.run(
    AgentHooks(
      post_response=review_post_response,
      post_tool_call=review_post_tool_call,
    )
  )
  stats.review_rounds = review_agent.meter.chat_rounds
  history = [
    {
      "phase": "analyze",
      "history": [asdict(message) for message in analyze_agent.get_history()],
    },
    {
      "phase": "review",
      "history": [asdict(message) for message in review_agent.get_history()],
    },
  ]
  return result, history


def validate_output_path(path_str: Optional[str], *, force: bool) -> Optional[Path]:
  if not path_str:
    return None
  path = Path(path_str).resolve()
  if path.exists() and not force:
    panic(f"Output file {path} already exists. Use --force to overwrite it.")
  path.parent.mkdir(parents=True, exist_ok=True)
  return path


def parse_args():
  parser = ArgumentParser(description="llvm-autoreview (archer style)")
  parser.add_argument("--pr", type=int, required=True, help="LLVM pull request ID.")
  parser.add_argument("--model", type=str, required=True, help="LLM model name.")
  parser.add_argument(
    "--driver",
    type=str,
    default="openai",
    choices=["openai", "anthropic"],
    help="LLM driver (default: openai).",
  )
  parser.add_argument(
    "--stats",
    type=str,
    default=None,
    help="Path to save run statistics JSON.",
  )
  parser.add_argument(
    "--history",
    type=str,
    default=None,
    help="Path to save chat history JSON.",
  )
  parser.add_argument(
    "--review",
    type=str,
    default=None,
    help="Path to save final markdown review report.",
  )
  parser.add_argument(
    "--refresh-pr-info",
    action="store_true",
    default=False,
    help="Force refreshing cached PR metadata from GitHub.",
  )
  parser.add_argument(
    "--debug",
    action="store_true",
    default=False,
    help="Enable debug mode.",
  )
  parser.add_argument(
    "--interactive",
    action="store_true",
    default=False,
    help="Enable ask tool for interactive clarifications.",
  )
  parser.add_argument(
    "--force",
    action="store_true",
    default=False,
    help="Allow overwrite of output files.",
  )
  return parser.parse_args()


def main():
  harness.require_home_dir()
  args = parse_args()

  if args.debug:
    global console
    console = get_boxed_console(debug_mode=True)

  if args.driver == "openai":
    from harness.lms.openai_generic import GPTGenericAgent

    driver_class = GPTGenericAgent
  elif args.driver == "anthropic":
    from harness.lms.anthropic_generic import ClaudeGenericAgent

    driver_class = ClaudeGenericAgent
  else:
    panic(f"Unsupported LLM API driver: {args.driver}")

  GlobalMeter.configure(
    token_limit=AGENT_MAX_CONSUMED_TOKENS,
    round_limit=AGENT_MAX_CHAT_ROUNDS,
  )

  aconf = AgentConfig(
    driver_class=driver_class,
    model=args.model,
    temperature=AGENT_TEMPERATURE,
    top_p=AGENT_TOP_P,
    max_completion_tokens=AGENT_MAX_COMPLETION_TOKENS,
    reasoning_effort=AGENT_REASONING_EFFORT,
    debug_mode=args.debug,
  )

  stats_path = validate_output_path(args.stats, force=args.force)
  history_path = validate_output_path(args.history, force=args.force)
  review_path = validate_output_path(args.review, force=args.force)

  pr_info = fetch_pr_info(args.pr, refresh=args.refresh_pr_info)
  stats = RunStats(command=vars(args))

  console.print(f"PR ID: #{pr_info.pr_id}")
  console.print(f"PR Title: {pr_info.title}")
  console.print(f"Components: {', '.join(pr_info.components) or '<unknown>'}")
  console.print(f"Base commit: {pr_info.base_commit}")
  console.print(f"Head commit: {pr_info.fix_commit}")

  review_result = None
  run_history = None
  stats.total_time_sec = time.time()
  try:
    with Harness.workspace() as h:
      setup_llvm_environment(pr_info, h)
      review_result, run_history = run_archer_agent(
        pr_info,
        aconf=aconf,
        h=h,
        stats=stats,
        interactive=args.interactive,
      )

      curate_new_insights(
        aconf=aconf,
        h=h,
        pr_info=pr_info,
        stats=stats,
        run_outcome="success",
      )
  except Exception as e:
    stats.error = type(e).__name__
    stats.errmsg = str(e)
    stats.traceback = traceback.format_exc()
    raise
  finally:
    stats.total_time_sec = time.time() - stats.total_time_sec
    gm = GlobalMeter.instance()
    stats.chat_rounds = gm.total_rounds
    stats.input_tokens = gm.total_input_tokens
    stats.output_tokens = gm.total_output_tokens
    stats.cached_tokens = gm.total_cached_tokens
    stats.total_tokens = gm.total_tokens

    if stats_path:
      with stats_path.open("w", encoding="utf-8") as fout:
        json.dump(stats.as_dict(), fout, indent=2)
      console.print(f"Generation statistics saved to {stats_path}.")

    if history_path and run_history is not None:
      with history_path.open("w", encoding="utf-8") as fout:
        json.dump(run_history, fout, indent=2)
      console.print(f"Chat history saved to {history_path}.")

    if review_path and stats.review_report:
      with review_path.open("w", encoding="utf-8") as fout:
        fout.write(stats.review_report)
      console.print(f"Review saved to {review_path}.")

  console.print("Final Review")
  console.print("------------")
  console.print(review_result or stats.review_report or "<empty>")
  console.print("Statistics")
  console.print("----------")
  console.print(json.dumps(stats.as_dict(), indent=2))


if __name__ == "__main__":
  main()
