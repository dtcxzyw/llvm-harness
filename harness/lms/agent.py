from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Literal, Optional, Tuple, Type, Union

from tenacity import (
  retry,
  stop_after_attempt,
  wait_random_exponential,
)  # for exponential backoff

from harness.lms.meter import AgentMeter, GlobalMeter, ReachRoundLimit, ReachTokenLimit
from harness.lms.skill import SkillTool, load_skill
from harness.lms.tool import (
  TOOL_SEARCH_NAME,
  DeferredToolWrapper,
  FuncToolBase,
  ToolRegistry,
)
from harness.utils.console import get_boxed_console
from harness.utils.text import spill_if_too_long

# ---------------------------------------------------------------------------
# Agent messages
# ---------------------------------------------------------------------------


@dataclass
class ChatMessage:
  type: Union[
    Literal["message"], Literal["function_call"], Literal["function_call_output"]
  ]


@dataclass
class ChatMessageMessage(ChatMessage):
  role: str = Union[Literal["system"], Literal["user"], Literal["assistant"]]
  content: str = ""
  type: str = "message"


@dataclass
class ChatMessageFunctionCall(ChatMessage):
  call_id: str = ""
  name: str = ""
  arguments: str = ""
  type: str = "function_call"


@dataclass
class ChatMessageFunctionCallOutput(ChatMessage):
  call_id: str = ""
  output: str = ""
  type: str = "function_call_output"


# ---------------------------------------------------------------------------
# Agent hooks
# ---------------------------------------------------------------------------

# pre_tool_call(name, args_dict) -> Tuple[bool, dict | str]
#   Return (True, args) to proceed (args may be modified), or
#   (False, response) to skip the call (a response is fed back to the model).
PreToolCallHook = Callable[[str, dict], Tuple[bool, dict | str]]

# post_response(content) -> Tuple[bool, str]
#   If flag is True, content is passed as user prompt for the next round.
#   Otherwise, content is returned as the final output (stops the loop).
PostResponseHook = Callable[[str], Tuple[bool, str]]
# post_tool_call(name, args_json, result) -> Tuple[bool, str]
#   If flag is True, content is passed to the assistant.
#   Otherwise, content is returned as the final output (stops the loop).
PostToolCallHook = Callable[[str, str, str], Tuple[bool, str]]


@dataclass
class AgentHooks:
  """Hooks for customizing agent loop behavior."""

  post_response: PostResponseHook
  post_tool_call: PostToolCallHook
  pre_tool_call: Optional[PreToolCallHook] = None


# ---------------------------------------------------------------------------
# Agent framework
# ---------------------------------------------------------------------------


ReasoningEffort = Literal[
  "NOT_GIVEN", "none", "minimal", "low", "medium", "high", "xhigh"
]


@dataclass(frozen=True)
class AgentConfig:
  """Immutable configuration for creating agents.

  Holds model parameters and the concrete agent class. Call create_agent()
  to get a fresh agent instance with its own history, tools, and meter.
  """

  driver_class: Type[AgentBase]
  model: str
  temperature: float = 0
  top_p: float = 0.95
  max_completion_tokens: int = 8092
  reasoning_effort: ReasoningEffort = "NOT_GIVEN"
  debug_mode: bool = False

  def create_agent(
    self,
    tools: List[Tuple[FuncToolBase, int] | Tuple[FuncToolBase, int, bool]]
    | None = None,
    skills: List[
      Tuple[Path, int, Optional[int]] | Tuple[Path, int, Optional[int], bool]
    ]
    | None = None,
  ) -> AgentBase:
    """Create a fresh agent instance from this configuration.

    Optionally register tools and skills in one call::

        config.create_agent(
          tools=[
            (ReadTool(), 250),           # pre-loaded (default)
            (EditTool(), 25),
            (OptTool(), 250, True),      # deferred — stub description
          ],
          skills=[
            (skill1_path, 10, 250),             # pre-loaded (default)
            (skill2_path, 10, None, True),      # deferred
          ],
        )

    Args:
      tools: List of (tool, call_budget) or (tool, call_budget, deferred)
        to register. When deferred is True the tool gets a stub description
        and a ``tool_search`` meta-tool is auto-created.
      skills: List of (skill_path, call_budget, per_tool_budget) or
        (skill_path, call_budget, per_tool_budget, deferred) to register.
        call_budget controls how many times the skill itself can be called.
        per_tool_budget overrides the tool-budget property in the skill's
        SKILL.md frontmatter, controlling the per-tool call limit inside the
        skill sub-loop. If per_tool_budget is None, keep the original budget.
    """
    agent = self.driver_class(self)
    for entry in tools or []:
      if len(entry) == 3:
        tool, budget, deferred = entry
        agent.register_tool(tool, budget, deferred=deferred)
      else:
        tool, budget = entry
        agent.register_tool(tool, budget)
    for entry in skills or []:
      if len(entry) == 4:
        path, budget, tool_budget, deferred = entry
        agent.register_skill(path, budget, tool_budget=tool_budget, deferred=deferred)
      else:
        path, budget, tool_budget = entry
        agent.register_skill(path, budget, tool_budget=tool_budget)
    return agent


class AgentBase:
  def __init__(self, config: AgentConfig):
    assert config.reasoning_effort in [
      "NOT_GIVEN",
      "none",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
    ], (
      f"Invalid reasoning_effort: {config.reasoning_effort}; "
      f"must be one of NOT_GIVEN, none, minimal, low, medium, high, and xhigh."
    )
    self.config = config
    self.model = config.model
    self.temperature = config.temperature
    self.top_p = config.top_p
    self.max_completion_tokens = config.max_completion_tokens
    self.reasoning_effort = config.reasoning_effort
    self.debug_mode = config.debug_mode
    self.history = []
    self.tools = ToolRegistry()
    self.meter: AgentMeter = GlobalMeter.instance().create_meter()
    self.console = get_boxed_console(debug_mode=config.debug_mode)

  def is_debug_mode(self):
    return self.debug_mode

  def enable_debug_mode(self):
    self.debug_mode = True
    self.console = get_boxed_console(debug_mode=True)

  def disable_debug_mode(self):
    self.debug_mode = False
    self.console = get_boxed_console(debug_mode=False)

  def register_tool(
    self,
    tool: FuncToolBase,
    budget: Optional[int] = None,
    deferred: bool = False,
  ):
    """Register a tool as callable by the agent.

    Args:
      tool: The tool object to register.
      budget: Max number of times this tool can be called before it's
        exhausted. None means unlimited.
      deferred: If True, the tool is registered with a stub description
        and a ``tool_search`` meta-tool is automatically created so the
        agent can discover the full description at runtime.
    """
    suffix = " [deferred]" if deferred else ""
    self.console.print(
      "Registering tool: "
      + tool.name()
      + " (budget="
      + ToolRegistry.format_budget(budget)
      + ")"
      + suffix
    )
    self.tools.register(tool, budget, deferred=deferred)
    return tool.name()

  def register_skill(
    self,
    path: Path,
    budget: Optional[int] = None,
    tool_budget: Optional[int] = None,
    deferred: bool = False,
  ) -> str:
    """Register a skill from a SKILL.md file as a callable tool.

    Args:
      path: Directory containing the skill definition (SKILL.md).
      budget: Max number of times the skill itself can be called.
        None means unlimited.
      tool_budget: If set, overrides the ``tool-budget`` property in
        the skill's SKILL.md frontmatter, controlling the per-tool call
        limit inside the skill sub-loop.
      deferred: If True, the skill is registered with a stub description
        and discoverable via ``tool_search``.
    """
    suffix = " [deferred]" if deferred else ""
    self.console.print(
      "Registering skill: "
      + path.name
      + " (budget="
      + ToolRegistry.format_budget(budget)
      + ")"
      + suffix
    )
    skill = load_skill(path)
    if tool_budget is not None:
      skill.budget = tool_budget
    self.register_tool(SkillTool(skill, self), budget, deferred=deferred)
    return skill.name

  @abstractmethod
  def run(
    self,
    hooks: AgentHooks,
  ) -> str:
    """
    Call to LLMs and execute all function calls until the model stops.
    """
    ...

  def get_history(self):
    return self.history

  def clear_history(self):
    self.history = []

  def append_system_message(self, content: str):
    self.history.append(ChatMessageMessage(role="system", content=content))
    self.console.printb(title="System", message=content)

  def append_user_message(self, content: str):
    self.history.append(ChatMessageMessage(role="user", content=content))
    self.console.printb(title="User", message=content)

  def append_assistant_message(self, content: str):
    self.history.append(ChatMessageMessage(role="assistant", content=content))
    self.console.printb(title="Assistant", message=content)

  def append_function_tool_call(self, call_id: str, name: str, arguments: str):
    self.history.append(
      ChatMessageFunctionCall(call_id=call_id, name=name, arguments=arguments)
    )
    self.console.printb(
      title=f"Function Call (id = {call_id})",
      message=f"{name}({arguments})",
    )

  def append_function_tool_call_output(self, call_id: str, result: str):
    self.history.append(ChatMessageFunctionCallOutput(call_id=call_id, output=result))
    self.console.printb(
      title=f"Function Call Output (id = {call_id})",
      message=result,
    )

  def perform_tool_call(self, tool_name: str, tool_args: dict) -> str:
    return spill_if_too_long(
      self.tools.call(tool_name, tool_args),
      file_prefix=f"toolcall_{tool_name}_",
      char_limit=15000,
      line_limit=500,
    )

  def _get_remaining_tools(self) -> list[FuncToolBase]:
    remaining = [self.tools.get(name) for name in self.tools.list(ignore_budget=False)]
    self.console.print(
      "Remaining tools: "
      + str(
        [
          f"{tool.name()}[{ToolRegistry.format_budget(self.tools.get_remaining_budget(tool.name()))}]"
          for tool in remaining
        ]
      )
    )
    return remaining

  def run_skill(
    self,
    skill_name: str,
    skill_inst: str,
    tool_names: List[str],
    tool_budget: Optional[int],
    context_aware: bool = True,
  ) -> str:
    """Run a skill in a freshly spawned agent.

    Creates a new agent from the same config, registers only the skill's
    tools, and runs the sub-loop. The outer agent is never mutated.

    When context_aware is True, the new agent's history is seeded with a
    copy of the outer agent's history, allowing it to use context from
    previous interactions.
    """
    from harness.lms.skill import SkillDoneTool, SkillTool

    sub = self.config.create_agent()

    # Seed history from outer agent if context-aware
    if context_aware:
      sub.history = self.history.copy()

    # Register tools for the sub-agent.  Deferred tools are re-registered
    # with deferred=True so the sub-agent gets its own tool_search.  The
    # parent's tool_search is skipped — the sub-agent's registry creates
    # a fresh one automatically.

    def _register_for_sub(name: str):
      tool_obj = self.tools.get(name)
      is_deferred = isinstance(tool_obj, DeferredToolWrapper)
      if isinstance(tool_obj, SkillTool):
        fresh_tool = tool_obj.for_agent(sub)
      else:
        fresh_tool = tool_obj.fresh()
      sub.register_tool(fresh_tool, tool_budget, deferred=is_deferred)

    if not tool_names:
      for name in self.tools.list():
        if name == skill_name or name == TOOL_SEARCH_NAME:
          continue  # Avoid registering the skill recursively or tool_search (auto-created)
        _register_for_sub(name)
    else:
      missing_tools = []
      for name in tool_names:
        if name == TOOL_SEARCH_NAME:
          continue  # tool_search is auto-created
        if self.tools.has(name):
          _register_for_sub(name)
        else:
          missing_tools.append(name)
      if missing_tools:
        self.console.print(
          f"Warning: The following tools required by the skill {skill_name} are "
          f"not registered in the outer agent and won't be available in the skill sub-loop: {missing_tools}",
          color="yellow",
        )
    sub.register_tool(SkillDoneTool(), 1)

    # Seed the skill invocation as user message
    sub.append_user_message(skill_inst)

    # Run sub-agent loop
    done_result = [None]

    def post_response(_: str):
      return True, "Please continue. Call the `skill_done` tool when finished."

    def post_tool_call(name: str, _: str, result: str):
      if name == "skill_done":
        done_result[0] = result
        return False, result
      return True, result

    try:
      sub.run(
        AgentHooks(post_response=post_response, post_tool_call=post_tool_call),
      )
    except (ReachRoundLimit, ReachTokenLimit):
      pass  # Budget exhausted

    result = done_result[0]
    if result is None:
      result = "Error: budget exhausted without producing a result"

    return result

  @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(3))
  def _completion_api_with_backoff(self, **kwargs):
    return self._completion_api(**kwargs)

  @abstractmethod
  def _completion_api(self, **kwargs):
    """
    Call the provider API to get the completion.
    """
    ...
