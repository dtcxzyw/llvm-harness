import json
import os
import shlex
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional

os.environ["LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX"] = (
  "1"  # Disable the default URL suffix for Anthropic models in litellm
)
os.environ["MSWEA_SILENT_STARTUP"] = "1"  # Silent startup
os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "3"  # Retry 3 times
from minisweagent import Model
from minisweagent.agents.default import DefaultAgent, Submitted
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.run.utils.save import save_traj

import harness
from autofix.mini import (
  AGENT_MAX_CHAT_ROUNDS,
  AGENT_MAX_COMPLETION_TOKENS,
  AGENT_MAX_CONSUMED_TOKENS,
  AGENT_TEMPERATURE,
  AGENT_TOP_P,
  MAX_TCS_HEAVYWEIGHT_TOOLS,
  NoAvailablePatchFound,
  ReachToolBudget,
  RunStats,
  add_common_args,
  build_agent_config,
  build_harness_from_args,
  configure_lit_test_dirs,
  log_issue_banner,
)
from harness.llvm.harness import Harness
from harness.lms.meter import GlobalMeter
from harness.lms.tool import FuncToolCallException
from harness.tools.bash import FORBIDDEN_TOOLS
from harness.utils import bashlex
from harness.utils.console import get_boxed_console

# TODO: remove duplicates with main.py
console = get_boxed_console(debug_mode=False)


def panic(msg: str):
  console.print(f"Error: {msg}", color="red")
  exit(1)


# TODO: Python etc can also edit files ...
EDITING_TOOLS = [
  "sed",
  "awk",
]


class MyModel(LitellmModel):
  def __init__(self, model: str, *, provider="openai"):
    super().__init__(
      model_name=model,
      model_kwargs={
        "custom_llm_provider": provider,
        "api_base": os.environ.get("LLVM_HARNESS_LM_API_ENDPOINT"),
        "api_key": os.environ.get("LLVM_HARNESS_LM_API_KEY"),
        "temperature": AGENT_TEMPERATURE,
        "top_p": AGENT_TOP_P,
        "max_completion_tokens": AGENT_MAX_COMPLETION_TOKENS,
        "drop_params": True,
      },
      cost_tracking="ignore_errors",  # Ignore cost tracking errors, we have our own
    )
    self.meter = GlobalMeter.instance().create_meter()

  def _query(self, messages, **kwargs):
    self.meter.record_round()

    response = super()._query(messages, **kwargs)

    console.print(GlobalMeter.format_status(self.meter))
    usage = getattr(response, "usage", None)
    if usage:
      cached = 0
      if usage.prompt_tokens_details:
        cached = usage.prompt_tokens_details.cached_tokens
      self.meter.record_usage(
        input_tokens=usage.prompt_tokens,
        cached_tokens=cached,
        output_tokens=usage.completion_tokens,
      )

    return response

  def query(self, messages, **kwargs):
    console.printb(message=messages[-1]["content"], title=messages[-1]["role"])
    response = super().query(messages, **kwargs)
    console.printb(message=response["content"], title="assistant")
    return response


class MyEnvironment(LocalEnvironment):
  def __init__(self, *, cwd: str):
    super().__init__(cwd=cwd)
    self.shim_path = os.path.join("/", "tmp", "mswe_myenv_shim.sh")
    self._create_shim()

  def _create_shim(self):
    # TODO: How to defend the models from accessing /usr/bin/xxx directly?
    # Shim script to set up the execution environment for mini-swe-agent
    shim_content = "#!/bin/bash\n"
    for cmd in FORBIDDEN_TOOLS:
      shim_content += f"""
{cmd}() {{
  echo "Error: You do not have permission to access the command '{cmd}'."
  return 1
}}
"""
    with open(self.shim_path, "w") as f:
      f.write(shim_content)
    os.chmod(self.shim_path, 0o755)

  def execute(
    self, command: str, cwd: str = "", *, timeout: int | None = None
  ) -> dict[str, str]:
    command = shlex.join(["bash", "-c", f". {self.shim_path} && {command}"])
    return super().execute(command, cwd, timeout=timeout)


class MyAgent(DefaultAgent):
  def __init__(
    self, model: Model, provider: str, stats: RunStats, workdir: str
  ) -> None:
    super().__init__(
      model=MyModel(
        model=model,
        provider=provider,
      ),
      env=MyEnvironment(cwd=workdir),
      # IMPORTANT: Configurations except for `agent` should be configured programmatically.
      **harness.load_yaml_config("autofix", "mswe.yaml")["agent"],
    )
    self.stats = stats
    self.harness: Harness | None = None
    self.tester = None
    self.test_budget = MAX_TCS_HEAVYWEIGHT_TOOLS
    self.edit_budget = MAX_TCS_HEAVYWEIGHT_TOOLS

  def setup(self, harness: Harness):
    self.harness = harness
    self.tester = harness.make_tool("llvm_test")

  def _test_submission(self) -> Optional[str]:
    # Save the test trajectory
    patch = self.harness.fixenv.dump_patch()
    try:
      res = self.tester.call()
    except FuncToolCallException as e:
      self.stats.test_traj.append((patch, False))
      return f"FAILURE\n\n{e}"  # Return the error message
    if res == "<success>":
      # We are successful, save the patch
      self.stats.test_traj.append((patch, True))
      self.stats.patch = patch
      return None  # Success
    self.stats.test_traj.append((patch, False))
    return res  # Return the error message

  def execute_action(self, action: dict) -> dict:
    if self.test_budget == 0:
      raise ReachToolBudget("llvm_test")
    if self.edit_budget == 0:
      raise ReachToolBudget("edit")
    tool = (action["action"] or "").split(" ", maxsplit=1)[0]
    for subtool in bashlex.get_commands(tool):
      if subtool in FORBIDDEN_TOOLS:
        return {
          "output": f"Error: You do not have permission to use command `{subtool}`.",
          "returncode": 1,
        }
    if tool == "submit-patch":
      self.test_budget -= 1
      errmsg = self._test_submission()
      if errmsg:
        return {"output": errmsg, "returncode": 1}
      raise Submitted("Patch generated successfully.")
    if tool in EDITING_TOOLS:
      # Fix: Our edit tools sed/awk may also not change anything
      self.edit_budget -= 0  # We do not decrease the budget for now
    return super().execute_action(action)

  def step(self):
    console.print(
      f"Remaining tools: [edit[{self.edit_budget}], test[{self.test_budget}]]"
    )
    return super().step()


def parse_args():
  parser = ArgumentParser(description="mini-swe-agent (llvm-autofix)")
  add_common_args(parser)
  parser.add_argument(
    "--model",
    type=str,
    required=True,
    help="The LLM model to use for the agent.",
  )
  parser.add_argument(
    "--stats",
    type=str,
    default=None,
    help="Path to save the generation statistics as a JSON file (default: None).",
  )
  parser.add_argument(
    "--driver",
    type=str,
    default="openai",
    help="The LLM API driver to use (default: openai).",
    choices=["openai", "anthropic"],
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
  return parser.parse_args()


def main():
  harness.require_home_dir()

  args = parse_args()

  if args.debug:
    global console
    console = get_boxed_console(debug_mode=True)

  GlobalMeter.configure(
    token_limit=AGENT_MAX_CONSUMED_TOKENS,
    round_limit=AGENT_MAX_CHAT_ROUNDS,
  )

  if args.stats:
    if Path(args.stats).exists():
      panic(f"Stats file {args.stats} already exists.")

  try:
    stats = RunStats(command=vars(args))

    try:
      harness_ctx = build_harness_from_args(
        args,
        agent_config=build_agent_config(args.driver, args.model, args.debug),
        aggressive_testing=args.aggressive_testing,
        do_print=console.print,
      )
    except ValueError as e:
      panic(str(e))

    with harness_ctx as h:
      agent = MyAgent(args.model, args.driver, stats, workdir=str(h.llvm_dir))
      agent.setup(h)

      log_issue_banner(h, args, log=console.print)
      console.print("Building LLVM and try reproducing the issue ...")
      issue = h.reproduce()
      console.print("Issue reproduced successfully.")

      # Narrow lit regression scope from the opt command (no backtrace here).
      configure_lit_test_dirs(
        h,
        issue.raw_command,
        log=console.print,
        warn=lambda msg: console.print(msg, color="yellow"),
      )

      stats.total_time_sec = time.time()
      console.print("Starting to fix the issue ...")
      exit_status, result = agent.run(
        "",
        issue_type=issue.bug_type,
        issue_rep_path=str(issue.file_path),
        issue_rep_code=issue.source,
        issue_command=" ".join(issue.command),
        issue_symptom=issue.symptom,
        forbidden_tools=", ".join(FORBIDDEN_TOOLS),
        workdir=str(h.llvm_dir),
      )
      if not stats.patch:
        raise NoAvailablePatchFound("All efforts tried yet no available patches found.")
      if not h.fixenv.use_entire_regression_test_suite:
        console.print("Post-validating the generated patch ...")
        passed, errmsg = h.post_validate()
        if not passed:
          stats.patch = None
          console.printb(title="Post-validation", message=errmsg)
          raise NoAvailablePatchFound("Post validation failed")
        console.print("Passed")
      extra_info = {}
  except Exception as e:
    import traceback

    stats.error = type(e).__name__
    stats.errmsg = str(e)
    stats.traceback = traceback.format_exc()

    exit_status = stats.error
    result = stats.errmsg
    extra_info = {"traceback": stats.traceback}

    raise e
  finally:
    gm = GlobalMeter.instance()
    stats.chat_rounds = gm.total_rounds
    stats.input_tokens = gm.total_input_tokens
    stats.output_tokens = gm.total_output_tokens
    stats.cached_tokens = gm.total_cached_tokens
    stats.total_tokens = gm.total_tokens
    stats.total_time_sec = time.time() - stats.total_time_sec
    if args.stats:
      with open(args.stats, "w") as fou:
        json.dump(stats.as_dict(), fou, indent=2)
      console.print(f"Generation statistics saved to {args.stats}.")
      agent.model.config.model_kwargs["api_base"] = "hidden"
      agent.model.config.model_kwargs["api_key"] = "hidden"
      save_traj(
        agent,
        Path(args.stats).with_suffix(".traj.json"),
        exit_status=exit_status,
        result=result,
        extra_info=extra_info,
      )

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
