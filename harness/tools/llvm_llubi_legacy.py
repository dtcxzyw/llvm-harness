from pathlib import Path

from harness.lms.tool import FuncToolCallException, FuncToolSpec, StatelessFuncToolBase
from harness.tools.llvm_llubi import run_llubi


class InterpretIrLegacyTool(StatelessFuncToolBase):
  def __init__(self, llubi_legacy_path: str):
    self._llubi = Path(llubi_legacy_path).resolve()
    if not self._llubi.is_file():
      raise FuncToolCallException(f"llubi_legacy not found at {self._llubi}")

  def spec(self) -> FuncToolSpec:
    return FuncToolSpec(
      "llvm_interpret_ir_legacy",
      "Interpret an LLVM IR file strictly following LLVM IR's semantics and return its output and exit code. "
      "Like `llvm_interpret_ir`, this checks immediate undefined behaviors during execution and handles poison "
      "values properly, with no JIT compilation. "
      "Use this when you want to check the semantics of an IR program. "
      "Use this ONLY for small programs, rather than large-scale software. "
      "Note 1: this uses the standalone (legacy) llubi binary from "
      "dtcxzyw/llvm-ub-aware-interpreter, installed independently of the LLVM source tree — "
      "so its behavior does NOT reflect local edits to the LLVM source. "
      "Note 2: prefer this over `llvm_interpret_ir` when working with LLVM versions before 23.0.0 "
      "or when you need a reference implementation independent of the local LLVM build.",
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
          "Optional arguments passed to llubi_legacy before the input file. "
          "Example: '--verbose' to print intermediate results for each instruction executed.",
        ),
      ],
      keywords=[
        "llubi",
        "llubi_legacy",
        "interpret",
        "ub",
        "undefined",
        "behavior",
        "poison",
        "ir",
        "semantics",
        "legacy",
      ],
    )

  def _call(self, *, input_path: str, args: str = "", **kwargs) -> str:
    return run_llubi(self._llubi, input_path, args)
