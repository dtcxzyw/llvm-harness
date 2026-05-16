import shlex
import subprocess
from pathlib import Path

from harness.lms.tool import FuncToolCallException, FuncToolSpec, StatelessFuncToolBase
from harness.tools.llvm_mixins import LlvmBuildDirMixin
from harness.utils.cmdline import spawn_process


def run_llubi(llubi: Path, input_path: str, args: str = "", timeout: int = 60) -> str:
  """Spawn ``llubi`` on ``input_path`` and format its result.

  Shared by :class:`InterpretIrTool` (in-tree LLVM 23+) and
  :class:`InterpretIrLegacyTool` (standalone). Raises
  :class:`FuncToolCallException` when the input file is missing.
  """
  input_file = Path(input_path)
  if not input_file.is_file():
    raise FuncToolCallException(f"Input file not found: {input_path}")

  proc = spawn_process(
    shlex.split(f"{llubi} {args.strip()} {input_file}"),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=timeout,
  )

  stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
  stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
  parts = [f"Exit code: {proc.returncode}"]
  if stdout.strip():
    parts.append(f"stdout:\n{stdout.rstrip()}")
  if stderr.strip():
    parts.append(f"stderr:\n{stderr.rstrip()}")
  return "\n".join(parts)


class InterpretIrTool(LlvmBuildDirMixin, StatelessFuncToolBase):
  def __init__(self, llvm_build_dir: str):
    LlvmBuildDirMixin.__init__(self, llvm_build_dir)
    self._llubi = self._binary_path("llubi")

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "llvm_interpret_ir",
      "Interpret an LLVM IR file strictly following LLVM IR's semantics and return its output and exit code. "
      "This tool is different from `llvm_execute_ir` in that it will check immediate undefined behaviors "
      "during execution and handle poison values properly. "
      "It does not have JIT compilation, neither. "
      "Use this when you want to check the semantics of an IR program. "
      "Use this ONLY for small programs, rather than large-scale software. "
      f"Note 1: uses the llubi binary built at {self.llvm_build_dir}, so its behavior reflects any local edits to the LLVM source. "
      "Note 2: this tool is available since LLVM 23.0.0; "
      "use the legacy version when unavailable.",
      [
        FuncToolSpec.Param(
          "input_path",
          "string",
          True,
          "Path to the LLVM IR file to interpret. Example: '/tmp/input.ll'.",
        ),
        FuncToolSpec.Param(
          "args",
          "string",
          False,
          "Optional arguments passed to llubi before the input file. "
          "Example: '--entry-function=test' to specify the entry function to interpret. "
          "By default, the entry function is 'main'. "
          "Example: '--verbose' to print intermediate results for each instruction executed.",
        ),
      ],
      keywords=[
        "llubi",
        "interpret",
        "ub",
        "undefined",
        "behavior",
        "poison",
        "ir",
        "semantics",
      ],
    )

  def _call(self, *, input_path: str, args: str = "", **kwargs) -> str:
    return run_llubi(self._llubi, input_path, args)
