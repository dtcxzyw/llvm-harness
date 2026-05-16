import shlex
import subprocess
from pathlib import Path

from harness.llvm.intern.llvm import is_opt_crash
from harness.lms.tool import FuncToolCallException, FuncToolSpec, StatelessFuncToolBase
from harness.tools.llvm_mixins import LlvmBuildDirMixin
from harness.utils.cmdline import spawn_process


class OptimizeIrTool(LlvmBuildDirMixin, StatelessFuncToolBase):
  def __init__(self, llvm_build_dir: str):
    LlvmBuildDirMixin.__init__(self, llvm_build_dir)
    self._opt = self._binary_path("opt")

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "llvm_optimize_ir",
      "Apply LLVM optimization passes to an LLVM IR file and return the transformed IR. "
      "Useful for reproducing a transformation, testing how a specific pass rewrites IR, "
      "or checking if opt crashes on a given input. "
      f"Note: uses the opt binary built at {self.llvm_build_dir}, so its behavior reflects any local edits to the LLVM source.",
      [
        FuncToolSpec.Param(
          "input_path",
          "string",
          True,
          "Path to the LLVM IR file to transform. Example: '/tmp/input.ll'.",
        ),
        FuncToolSpec.Param(
          "args",
          "string",
          True,
          "Arguments passed to opt, including passes and output options. "
          "Example: '-S -passes=instcombine' or '-S -passes=instcombine -o /tmp/output.ll'.",
        ),
      ],
      keywords=["opt", "transform", "pass", "optimization", "ir"],
    )

  def _call(self, *, input_path: str, args: str, **kwargs) -> str:
    input_file = Path(input_path)
    if not input_file.is_file():
      raise FuncToolCallException(f"Input file not found: {input_path}")

    proc = spawn_process(
      shlex.split(f"{self._opt} {args} {input_file}"),
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      timeout=60,
    )

    if proc.returncode == 0:
      stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
      return stdout if stdout else "Success: output written to file."

    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if is_opt_crash(err):
      raise FuncToolCallException(f"opt crashed:\n{err}")
    raise FuncToolCallException(f"opt failed: {err}")
