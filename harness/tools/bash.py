import shlex
from subprocess import CalledProcessError

from harness.llvm.access import AccessControl
from harness.lms.tool import FuncToolCallException, FuncToolSpec, StatelessFuncToolBase
from harness.utils import bashlex, cmdline

# TODO: add other tools that do not require permission
FORBIDDEN_TOOLS = [
  "which",
  "sudo",
  "rm",
  "curl",
  "wget",
  "git",
  "ssh",
  "scp",
  "ftp",
  "telnet",
  "ping",
  "traceroute",
  "nslookup",
  "dig",
  "nmap",
  "apt",
  "apt-get",
  "dpkg",
]


class BashTool(StatelessFuncToolBase):
  def __init__(self, acl: AccessControl):
    self.acl = acl

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "bash",
      "Execute a shell command and returns its output. "
      "Avoid this tool for dedicated tasks (e.g., reading/writing files, building/testing LLVM, etc.), "
      "unless you have verified that a dedicated tool (e.g., `read`, and `llvm_test`) cannot accomplish your task. "
      "Use this tool for tasks "
      "requiring chaining commands with pipes (NO other tool supports this), "
      "running shell scripts you wrote to accomplish a specific task, "
      "executing binaries found but not covered by other tools, or "
      "other shell tasks not supported elsewhere. "
      "Some commands (git, rm, curl, etc.) are restricted.",
      [
        FuncToolSpec.Param(
          "command",
          "string",
          True,
          "The bash command to execute.",
        ),
        FuncToolSpec.Param(
          "cwd",
          "string",
          False,
          "Optional absolute path to use as the working directory for the command.",
        ),
        FuncToolSpec.Param(
          "timeout",
          "integer",
          False,
          "Optional timeout in seconds for the command execution. Default is 60 seconds.",
        ),
      ],
      keywords=["shell", "command", "terminal", "execute"],
    )

  def _call(
    self, *, command: str, cwd: str | None = None, timeout: int = 60, **kwargs
  ) -> str:
    if not command:
      raise FuncToolCallException(
        "No command provided. Please specify the bash command to execute."
      )

    # Check for forbidden tools in the command to prevent unauthorized actions.
    for cmd in bashlex.get_commands(command):
      if cmd in FORBIDDEN_TOOLS:
        raise FuncToolCallException(
          f"You do not have permission to use command `{cmd}`."
        )

    # Validate cwd if provided.
    work_dir = None
    if cwd:
      work_dir = self.acl.check_readable_dir(cwd)

    # We use 'bash -c' to ensure full shell support (pipes, redirects, etc.)
    bash_cmd = f"bash -c {shlex.quote(command)}"

    try:
      output = cmdline.getoutput(bash_cmd, cwd=work_dir, check=True, timeout=timeout)
      return output.decode("utf-8", errors="replace")
    except CalledProcessError as e:
      # If the command failed, return the combined output and error message.
      error_output = e.stdout.decode("utf-8") if e.stdout else ""
      raise FuncToolCallException(
        f"Command failed with exit code {e.returncode}.\n\nOutput:\n{error_output}"
      )
    except Exception as e:
      raise FuncToolCallException(f"Failed to execute command: {str(e)}")
