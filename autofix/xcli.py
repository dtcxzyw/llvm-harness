import json
import os
import shlex
import shutil
import threading
import time
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from uuid import uuid4 as uuid

import harness
from autofix.mini import (
  NoAvailablePatchFound,
  RunStats,
  add_common_args,
  build_harness_from_args,
  configure_lit_test_dirs,
  log_issue_banner,
)
from harness.llvm.harness import Harness
from harness.lms.agent import AgentConfig
from harness.lms.openai_generic import GPTGenericAgent
from harness.lms.tool import FuncToolCallException
from harness.utils import cmdline

_TEST_SERVER_ADDR = "127.0.0.1"
_TEST_SERVER_PORT = 3921

PROMPT_TEMPLATE = harness.load_yaml_config("autofix", "xcli.yaml")["prompt"]


def panic(msg: str):
  print(f"Error: {msg}")
  exit(1)


def parse_args():
  parser = ArgumentParser(description="Wrapper of XXX CLI/Agent (llvm-autofix)")
  add_common_args(parser)
  parser.add_argument(
    "--xcli",
    type=str,
    required=True,
    choices=["claudecode"],
    help="The XXX CLI/Agent to use for fixing the issue.",
  )
  parser.add_argument(
    "--model",
    type=str,
    default=None,
    help="The LLM model to use for the agent.",
  )
  parser.add_argument(
    "--stats",
    type=str,
    required=True,
    help="Path to save the generation statistics as a JSON file.",
  )
  parser.add_argument(
    "--aggressive-testing",
    action="store_true",
    default=False,
    help="Use all Transforms and Analysis tests for testing patches online (default: False).",
  )
  return parser.parse_args()


def start_test_server(harness: Harness, stats: RunStats):
  """
  Start HTTP server to serve the test tool and return the commands to request the server.
  """
  tester = harness.make_tool("llvm_test")

  def do_test():
    patch = harness.fixenv.dump_patch()
    try:
      res = tester.call()
    except FuncToolCallException as e:
      stats.test_traj.append((patch, False))
      return f"FAILURE\n\n{e}"
    if res == "<success>":
      stats.test_traj.append((patch, True))
      stats.patch = patch
      return "SUCCESS"
    stats.test_traj.append((patch, False))
    return res

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      result = do_test()
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.end_headers()
      self.wfile.write(result.encode())

    def log_message(self, format, *args):
      pass  # Suppress request logging

  server = HTTPServer((_TEST_SERVER_ADDR, _TEST_SERVER_PORT), Handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()

  script = f"""#!/bin/bash
RESULT=$(curl -s -X POST http://{_TEST_SERVER_ADDR}:{_TEST_SERVER_PORT}/)
if [ "$RESULT" = "SUCCESS" ]; then
  exit 0
else
  echo "$RESULT"
  exit 1
fi
"""
  return script, server


def ensure_xcli_exists(xcli: str):
  bin = {
    "claudecode": "claude",
  }.get(xcli, "unknown")
  if bin == "unknown":
    panic(f"Unsupported X-CLI: {xcli}")
  if not shutil.which(bin):
    panic(f"The `{bin}` command is not found.")


def render_xcli_command(
  xcli: str,
  *,
  prompt: str,
  session: Optional[str] = None,
  model: Optional[str] = None,
) -> str:
  # TODO: Output the trajectory in a structured format
  if xcli == "claudecode":
    model_arg = f"--model {model}" if model else ""
    session_arg = f"--session-id {session}" if session else ""
    return f"claude --dangerously-skip-permissions -p --output-format json {model_arg} {session_arg} {shlex.quote(prompt)}"
  # TODO: Support Codex and Gemini CLI
  raise ValueError(f"Unsupported X-CLI: {xcli}")


def save_xcli_trajectory(
  xcli: str, *, session: str, summary: str, stats: RunStats, stats_path: Path
):
  assert xcli == "claudecode", (
    f"Support for other X-CLI ({xcli}) has not been implemented"
  )

  sum_dict = json.loads(summary)
  # Update stats
  stats.chat_rounds = sum_dict["num_turns"]
  stats.output_tokens = sum_dict["usage"]["output_tokens"]
  stats.cached_tokens = (
    sum_dict["usage"]["cache_creation_input_tokens"]
    + sum_dict["usage"]["cache_read_input_tokens"]
  )
  stats.input_tokens = sum_dict["usage"]["input_tokens"] + stats.cached_tokens
  stats.total_tokens = stats.input_tokens + stats.output_tokens
  # Save the summary
  with stats_path.with_suffix(".summary.json").open("w") as fou:
    json.dump(sum_dict, fou, indent=2)

  # Find and save the trajectory
  proj_name = "-".join(str(Path(harness.home_dir).resolve().absolute()).split("/"))
  # Trajectory of CC is saved at:
  # - ~/.claude/projects/{proj_name}/{session} or
  # ~/.claude/projects/{proj_name}/{session}.jsonl
  proj_dir = Path.home() / ".claude" / "projects" / proj_name

  traj_jsonl = proj_dir / (session + ".jsonl")
  if traj_jsonl.exists() and traj_jsonl.is_file():
    shutil.copy2(traj_jsonl, stats_path.with_suffix(".traj.jsonl"))
    return

  traj_dir = proj_dir / session
  if traj_dir.exists() and traj_dir.is_dir():
    shutil.copytree(traj_dir, stats_path.with_suffix(".traj"), dirs_exist_ok=True)
    return

  print(
    f"[WARNING] No trajectory file found for X-CLI {xcli}: neither {traj_jsonl} nor {traj_dir}"
  )


def main():
  harness.require_home_dir()

  args = parse_args()
  if args.autoreduce:
    raise NotImplementedError(
      "The --autoreduce option is not yet supported in XCLI mode currently."
    )

  ensure_xcli_exists(args.xcli)
  print(f"Preparing {args.xcli} command to fix the LLVM issue ...")

  stats_path = Path(args.stats).resolve().absolute()
  if stats_path.exists():
    panic(f"Stats file {args.stats} already exists.")

  try:
    harness_ctx = build_harness_from_args(
      args,
      aggressive_testing=args.aggressive_testing,
      agent_config=AgentConfig(model="gpt-4o", driver_class=GPTGenericAgent),
      do_print=print,
    )
  except ValueError as e:
    panic(str(e))

  with harness_ctx as h:
    log_issue_banner(h, args, log=print)
    print("Building LLVM and try reproducing the issue ...")
    issue = h.reproduce()
    print("Issue reproduced successfully.")

    # Narrow lit regression scope from the opt command (no backtrace here).
    configure_lit_test_dirs(h, issue.raw_command, log=print)

    prompt = PROMPT_TEMPLATE.format(
      issue_type=issue.bug_type,
      issue_rep_path=str(issue.file_path),
      issue_rep_code=issue.source,
      issue_command=" ".join(issue.command),
      issue_symptom=issue.symptom,
      llvm_dir=str(h.llvm_dir),
      build_dir=str(h.build_dir),
      llvm_alive_tv=str(h.alive_tv_path),
    )

    session = str(uuid())
    command = render_xcli_command(
      args.xcli,
      prompt=prompt,
      model=args.model,
      session=session,
    )
    print(f"Agent command prepared: {command[:80]} ...")

    stats = RunStats(command=vars(args))
    test_commands, test_server = start_test_server(h, stats)

    # Write submit-patch directly into a temp bin dir and add it to PATH
    tmp_bin = os.path.join("/", "tmp", "llvm-autofix-bin")
    os.makedirs(tmp_bin, exist_ok=True)
    submit_patch_script = os.path.join(tmp_bin, "submit-patch")
    with open(submit_patch_script, "w") as fou:
      fou.write(test_commands)
    os.chmod(submit_patch_script, 0o755)
    env = os.environ.copy()
    env["PATH"] = tmp_bin + ":" + env.get("PATH", "")

    print("Starting to fix the issue ...")
    stats.total_time_sec = time.time()
    try:
      summary = cmdline.check_output(command, timeout=1800, env=env)
      save_xcli_trajectory(
        args.xcli, session=session, summary=summary, stats=stats, stats_path=stats_path
      )
      if not stats.patch:
        raise NoAvailablePatchFound("All efforts tried yet no available patches found.")
      if not h.fixenv.use_entire_regression_test_suite:
        print("Post-validating the generated patch ...")
        passed, errmsg = h.post_validate()
        if not passed:
          stats.patch = None
          print("Post-validation failed:", errmsg)
          raise NoAvailablePatchFound("Post validation failed")
        print("Passed")
    except Exception as e:
      import traceback

      stats.error = type(e).__name__
      stats.errmsg = str(e)
      stats.traceback = traceback.format_exc()

      raise e
    finally:
      test_server.shutdown()
      stats.total_time_sec = time.time() - stats.total_time_sec
      with stats_path.open("w") as fou:
        json.dump(stats.as_dict(), fou, indent=2)
      print(f"Generation statistics saved to {stats_path}.")

  print("\n\nFinal Patch")
  print("-----------")
  print(stats.patch)
  print("Reference Patch")
  print("---------------")
  print(h.fixenv.get_reference_patch())
  print("Statistics")
  print("----------")
  print(json.dumps(stats.as_dict(), indent=2))


if __name__ == "__main__":
  main()
