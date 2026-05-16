from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import harness
from harness.llvm.access import AccessControl
from harness.llvm.debugger import DebuggerBase
from harness.llvm.intern import llvm as llvm_ops
from harness.llvm.intern.lab_env import FixEnv
from harness.llvm.intern.llvm_code import LlvmCode
from harness.llvm.issue import IssueCard, Reproducer, parse_lit_reproducer
from harness.lms.tool import FuncToolBase
from harness.utils import cmdline


@dataclass
class ReprodRes:
  """Result of running a reproducer."""

  bug_type: str  # "crash" | "miscompilation" | "hang"
  file_path: Path  # path to written .ll file
  command: list[str]  # resolved opt command tokens
  raw_command: str  # original unresolved command string
  source: str  # IR reproducer text
  symptom: str  # rendered log output


def _parse_raw_command(raw_cmd: str, reprod_file: str, opt_binary: str) -> list[str]:
  """Turn an opt test command template into a resolved command list.

  This consolidates the duplicated command-string construction from
  mini.py / mswe.py / xcli.py / autoreview.
  """
  return list(
    filter(
      lambda x: x != "",
      raw_cmd.replace("< ", " ")
      .replace("%s", reprod_file)
      .replace("2>&1", "")
      .replace("'", "")
      .replace('"', "")
      .replace("opt", opt_binary, 1)
      .strip()
      .split(" "),
    )
  )


def _make_temp_ll(issue_id: str, content: str) -> Path:
  """Write *content* to a uniquely-named temp ``.ll`` file and return its path."""
  fd, path = tempfile.mkstemp(suffix=".ll", prefix=f"reprod_{issue_id}_")
  try:
    os.write(fd, content.encode())
  finally:
    os.close(fd)
  return Path(path)


def _llvm_root() -> str:
  """Resolved absolute path to the LLVM source tree (single source of truth)."""
  return str(Path(llvm_ops.llvm_dir).resolve())


# ---------------------------------------------------------------------------
# ACL presets
# ---------------------------------------------------------------------------
# Each preset returns ``(editable, readable, ignored)`` lists of absolute
# path patterns.  They are callables because ``llvm_dir`` is resolved from
# environment variables at runtime.

AclPreset = Literal["llvm", "llvm+clang"]

_ACL_PRESETS: dict[str, callable] = {}


def _register_acl_preset(name: str):
  """Decorator that registers an ACL preset."""

  def _register(fn):
    _ACL_PRESETS[name] = fn
    return fn

  return _register


@_register_acl_preset("llvm")
def _acl_midend() -> tuple[list[str], list[str], list[str]]:
  """Middle-end focus: read the whole llvm/ tree, edit lib/ and include/."""
  r = _llvm_root()
  return (
    [f"{r}/llvm/lib", f"{r}/llvm/include", "/tmp"],  # editable
    [f"{r}/llvm", "/tmp"],  # readable
    [],  # ignored
  )


@_register_acl_preset("llvm+clang")
def _acl_fullend() -> tuple[list[str], list[str], list[str]]:
  """Full LLVM: read and edit everything under the project root."""
  r = _llvm_root()
  return (
    [
      f"{r}/llvm/lib",
      f"{r}/llvm/include",
      f"{r}/clang/lib",
      f"{r}/clang/include",
      "/tmp",
    ],  # editable
    [f"{r}/llvm", f"{r}/clang", "/tmp"],  # readable
    [],  # ignored
  )


DEFAULT_ACL_PRESET: AclPreset = "llvm"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Harness:
  """Configured LLVM workspace — the single entry point for harness consumers.

  Use one of the factory methods to create an instance:

  * :meth:`workspace` — bare LLVM workspace (superopt, general dev)
  * :meth:`from_issue_card` — from an :class:`IssueCard`
  * :meth:`from_issue_id` — bench issue from ``bench/``
  * :meth:`from_reproducer` — ad-hoc bug from a user-provided file
  """

  def __init__(
    self,
    *,
    acl_preset: AclPreset | None = None,
    acl_extras: tuple[list[str], list[str], list[str]] | None = None,
    fixenv: FixEnv | None = None,
    issue_id: str | None = None,
  ):
    self._acl_preset = acl_preset
    self._acl_extras = acl_extras or ([], [], [])
    self.acl = Harness._make_acl(self._acl_preset, *self._acl_extras)
    self.fixenv: FixEnv | None = fixenv
    self._issue_id = issue_id
    self._llvmcode: LlvmCode | None = None
    self._debugger: DebuggerBase | None = None

  # -------------------------------------------------------------------
  # Paths — delegated to intern/llvm.py (single source of truth)
  # -------------------------------------------------------------------

  @property
  def llvm_dir(self) -> Path:
    """Root of the LLVM source tree."""
    return Path(llvm_ops.llvm_dir).resolve()

  @property
  def build_dir(self) -> Path:
    """Current LLVM build directory."""
    return Path(llvm_ops.get_llvm_build_dir()).resolve()

  @staticmethod
  def _make_acl(
    preset: AclPreset | None = None,
    extra_editable: list[str] | None = None,
    extra_readable: list[str] | None = None,
    extra_ignored: list[str] | None = None,
  ) -> AccessControl:
    """Build an ACL from a named preset plus caller-supplied extras.

    The build directory is added automatically based on the current
    value of ``get_llvm_build_dir()`` so that it reflects any changes
    made by ``__enter__``.
    """
    name = preset or DEFAULT_ACL_PRESET
    editable, readable, ignored = _ACL_PRESETS[name]()
    build_dir = str(Path(llvm_ops.get_llvm_build_dir()).resolve())
    skills_dir = str(Path(harness.require_home_dir()) / "harness" / "skills")
    return AccessControl(
      editable=editable + [build_dir] + (extra_editable or []),
      readable=readable + [build_dir, skills_dir] + (extra_readable or []),
      ignored=ignored + (extra_ignored or []),
    )

  @property
  def alive_tv_path(self) -> Path:
    """Path to the alive-tv binary."""
    return Path(llvm_ops.llvm_alive_tv).resolve()

  @property
  def llubi_legacy_path(self) -> Path:
    """Path to the (legacy) llubi binary."""
    return Path(llvm_ops.llvm_llubi_legacy).resolve()

  @property
  def llvmcode(self) -> LlvmCode:
    """Lazily-created :class:`LlvmCode` for LLVM source analysis."""
    if self._llvmcode is None:
      self._llvmcode = LlvmCode()
    return self._llvmcode

  # -------------------------------------------------------------------
  # LLVM Operations
  # -------------------------------------------------------------------

  def run_opt(
    self, file, args: list[str], *, check: bool = True, **kwargs
  ) -> tuple[str, str]:
    """Run ``opt`` on *file* with *args* and return ``(command, output)``."""
    opt = self.build_dir / "bin" / "opt"
    cmd = " ".join([str(opt.absolute())] + args + [str(Path(file).absolute())])
    return cmd, cmdline.getoutput(cmd, check=check, **kwargs).decode()

  def run_alive2(
    self, src: str, tgt: str, additional_args: str = "", repro: bool = False
  ) -> tuple[str, str]:
    """Run alive2 on *src* → *tgt* and return ``(command, output)``."""
    alive_tv = str(self.alive_tv_path)
    alive2_args = f"--disable-undef-input --smt-to=60000 {additional_args}".strip()
    cmd = f"{alive_tv} {alive2_args}"
    success, log = llvm_ops.alive2_check(src, tgt, alive2_args, repro)
    if isinstance(log, dict):
      log = log.get("log", "")
    prefix = "SUCCESS" if success else "FAILURE"
    return cmd, f"{prefix}: {log}"

  # -------------------------------------------------------------------
  # Debugger
  # -------------------------------------------------------------------

  @property
  def debugger(self) -> DebuggerBase | None:
    """The attached debugger, or ``None``."""
    return self._debugger

  def attach_debugger(self, command: list[str]) -> DebuggerBase:
    """Create and attach a GDB debugger with the given command."""
    from harness.llvm.gdb_support import GDB

    self._debugger = GDB(command)
    return self._debugger

  # -------------------------------------------------------------------
  # Factory methods
  # -------------------------------------------------------------------

  @staticmethod
  def workspace(
    *,
    acl_preset: AclPreset | None = None,
    extra_editable: list[str] | None = None,
    extra_readable: list[str] | None = None,
    extra_ignored: list[str] | None = None,
  ) -> Harness:
    """Create a bare LLVM workspace (superopt, general dev)."""
    extras = (extra_editable or [], extra_readable or [], extra_ignored or [])
    return Harness(
      acl_preset=acl_preset,
      acl_extras=extras,
    )

  @staticmethod
  def from_issue_card(
    card: IssueCard,
    *,
    cmake_args: list[str] | None = None,
    max_build_jobs: int | None = None,
    max_test_jobs: int | None = None,
    aggressive_testing: bool = False,
    test_commit_checkout_changed_files_only: bool = False,
    reference_patch: str | None = None,
    issue_id: str | None = None,
    acl_preset: AclPreset | None = None,
    extra_editable: list[str] | None = None,
    extra_readable: list[str] | None = None,
    extra_ignored: list[str] | None = None,
  ) -> Harness:
    """Create a harness from an :class:`IssueCard`."""
    extras = (extra_editable or [], extra_readable or [], extra_ignored or [])
    env = FixEnv(
      card,
      additional_cmake_args=cmake_args or [],
      max_build_jobs=max_build_jobs or os.environ.get("LLVM_HARNESS_MAX_BUILD_JOBS"),
      max_test_jobs=max_test_jobs,
      use_entire_regression_test_suite=aggressive_testing,
      test_commit_checkout_changed_files_only=test_commit_checkout_changed_files_only,
      reference_patch=reference_patch,
    )
    return Harness(
      acl_preset=acl_preset,
      acl_extras=extras,
      fixenv=env,
      issue_id=issue_id,
    )

  @staticmethod
  def from_issue_id(
    issue_id: str,
    *,
    base_commit: str | None = None,
    cmake_args: list[str] | None = None,
    max_build_jobs: int | None = None,
    max_test_jobs: int | None = None,
    aggressive_testing: bool = False,
    acl_preset: AclPreset | None = None,
    extra_editable: list[str] | None = None,
    extra_readable: list[str] | None = None,
    extra_ignored: list[str] | None = None,
  ) -> Harness:
    """Create a harness for a bench issue from ``bench/``.

    ``base_commit`` overrides the bench-provided base commit when given.
    """
    data = llvm_ops.load_benchmark_issue(issue_id)
    hints = data.get("hints", {})
    card = IssueCard(
      bug_type=data["bug_type"],
      reproducers=[
        Reproducer(file=t["file"], commands=t["commands"], tests=t["tests"])
        for t in data["tests"]
      ],
      base_commit=base_commit or data.get("base_commit"),
      test_commit=data.get("test_commit", hints.get("fix_commit")),
      lit_test_dir=data.get("lit_test_dir"),
      issue=data.get("issue"),
    )
    return Harness.from_issue_card(
      card,
      cmake_args=cmake_args,
      max_build_jobs=max_build_jobs,
      max_test_jobs=max_test_jobs,
      aggressive_testing=aggressive_testing,
      test_commit_checkout_changed_files_only=data.get(
        "test_commit_checkout_changed_files_only", False
      ),
      reference_patch=data.get("patch"),
      issue_id=issue_id,
      acl_preset=acl_preset,
      extra_editable=extra_editable,
      extra_readable=extra_readable,
      extra_ignored=extra_ignored,
    )

  @staticmethod
  def from_reproducer(
    file: str | Path,
    *,
    base_commit: str | None = None,
    cmake_args: list[str] | None = None,
    max_build_jobs: int | None = None,
    max_test_jobs: int | None = None,
    aggressive_testing: bool = False,
    acl_preset: AclPreset | None = None,
    extra_editable: list[str] | None = None,
    extra_readable: list[str] | None = None,
    extra_ignored: list[str] | None = None,
  ) -> Harness:
    """Create a harness for an ad-hoc bug from a lit-style ``.ll`` file.

    The file must embed both directives:

    * ``; BUG: crash`` or ``; BUG: miscompilation``
    * ``; RUN: opt …``
    """
    reproducer, bug_type = parse_lit_reproducer(file)
    card = IssueCard(
      bug_type=bug_type,
      reproducers=[reproducer],
      base_commit=base_commit,
    )
    return Harness.from_issue_card(
      card,
      cmake_args=cmake_args,
      max_build_jobs=max_build_jobs,
      max_test_jobs=max_test_jobs,
      aggressive_testing=aggressive_testing,
      acl_preset=acl_preset,
      extra_editable=extra_editable,
      extra_readable=extra_readable,
      extra_ignored=extra_ignored,
    )

  # -------------------------------------------------------------------
  # Context manager
  # -------------------------------------------------------------------

  def __enter__(self) -> Harness:
    # For bench issues, set build dir to a per-issue subdirectory.
    if self._issue_id:
      llvm_ops.set_llvm_build_dir(
        os.path.join(llvm_ops.get_llvm_build_dir(), self._issue_id)
      )
      self.fixenv.reset()

    # Rebuild ACL now that the build dir is finalized.
    self.acl = Harness._make_acl(self._acl_preset, *self._acl_extras)

    return self

  def __exit__(self, *exc):
    pass

  # -------------------------------------------------------------------
  # Operations
  # -------------------------------------------------------------------

  def build(self) -> tuple[bool, str]:
    """Build LLVM. Delegates to ``fixenv.build()`` if available, otherwise
    calls the raw build function directly."""
    if self.fixenv is not None:
      return self.fixenv.build()

    return llvm_ops.build(
      max_build_jobs=os.cpu_count(),
    )

  def post_validate(self) -> tuple[bool, str]:
    """Run full regression tests to validate a patch.

    Temporarily enables the entire regression test suite, runs midend +
    regression-diff checks, then restores the original setting.

    Returns ``(passed, errmsg)``.
    """
    if self.fixenv is None:
      return True, "No fixenv configured, skipping post-validation."
    backup_val = self.fixenv.use_entire_regression_test_suite
    self.fixenv.use_entire_regression_test_suite = True
    try:
      passed, errmsg = self.fixenv.check_midend()
      if passed and self.fixenv.get_reference_patch() is not None:
        passed, errmsg = self.fixenv.check_regression_diff()
      return passed, errmsg
    finally:
      self.fixenv.use_entire_regression_test_suite = backup_val

  def reproduce(self) -> ReprodRes:
    """Reproduce the configured bug and return a :class:`ReprodRes`.

    Builds LLVM and runs the reproducer test(s); the first failing test
    becomes the :class:`ReprodRes` payload.
    """
    if self.fixenv is None:
      raise RuntimeError(
        "No issue configured. Use Harness.from_issue_card(), from_issue_id(), or from_reproducer()."
      )

    check_failed, check_log = self.fixenv.check_fast()
    if check_failed:
      raise RuntimeError(f"Failed to build or reproduce the issue.\n\n{check_log}")

    reprod_data = llvm_ops.get_first_failed_test(check_log)
    raw_cmd = reprod_data["args"]
    reprod_code = reprod_data["body"]
    reprod_log = llvm_ops.pretty_render_log(reprod_data["log"])

    # Write the reproducer IR to a uniquely-named temp file.
    reprod_file = _make_temp_ll(self._issue_id or "adhoc", reprod_code)

    opt_binary = str(self.build_dir / "bin" / "opt")
    command = _parse_raw_command(raw_cmd, str(reprod_file), opt_binary)

    return ReprodRes(
      bug_type=self.fixenv.get_bug_type(),
      file_path=reprod_file,
      command=command,
      raw_command=raw_cmd,
      source=reprod_code,
      symptom=reprod_log,
    )

  # -------------------------------------------------------------------
  # Git operations
  # -------------------------------------------------------------------

  def git(self, *args: str) -> str:
    """Run a git command in the LLVM source directory."""
    return llvm_ops.git_execute(list(args))

  def checkout(self, ref: str):
    """Checkout a git ref in the LLVM source directory."""
    self.git("checkout", ref)

  def reset_state(self):
    """Reset the LLVM source directory to a clean state."""
    llvm_ops.reset()

  def apply_patch(self, patch: str) -> tuple[bool, str]:
    """Apply a unified diff patch to the LLVM source tree."""
    return llvm_ops.apply_patch(patch)

  # -------------------------------------------------------------------
  # Skills
  # -------------------------------------------------------------------

  def get_skills(self) -> list[Path]:
    """All available skill directories (auto-discovered)."""
    from harness.skills import list_skills

    return list_skills()

  def get_skill(self, name: str) -> Path:
    """Return the path of a skill by directory name.

    Raises :class:`KeyError` if the skill is not found.
    """
    skills = self.get_skills()
    for sk in skills:
      if sk.name == name:
        return sk
    available = [sk.name for sk in skills]
    raise KeyError(
      f"Skill {name!r} not found. Available skills: {', '.join(available)}"
    )

  def install_skill(self, name: str, path: Path, exists_ok: bool = False):
    """Install a skill into *path*/skills under the name *name*."""

    skill_path = self.get_skill(name)

    target_path = path / "skills"
    target_path.mkdir(exist_ok=True)

    target_skill_path = target_path / name
    if target_skill_path.exists():
      if not exists_ok:
        raise FileExistsError(
          f"Target skill path {target_skill_path} already exists. Set exists_ok=True to overwrite."
        )
      if target_skill_path.is_symlink() or target_skill_path.is_file():
        target_skill_path.unlink()
      elif target_skill_path.is_dir():
        import shutil

        shutil.rmtree(target_skill_path)
    target_skill_path.symlink_to(skill_path, target_is_directory=True)

  # -------------------------------------------------------------------
  # Tools
  # -------------------------------------------------------------------

  def make_tools(self) -> list[FuncToolBase]:
    """Instantiate all tools available given the current harness state.

    Tools are gated by available dependencies:

    * Always (llvm_dir): read, list, find, ripgrep, edit, write, bash, insight
    * build_dir present: llvm_optimize_ir, llvm_compile_ir, llvm_execute_ir, llvm_interpret_ir, llvm_check_optim
    # alive2 present: llvm_verify_ir, llvm_verify_optim
    * fixenv present (bench issue): llvm_build, llvm_test, llvm_reset, llvm_preview_patch
    * debugger attached: llvm_code, llvm_docs, llvm_debug, llvm_eval_expr, llvm_langref
    """
    tools: list[FuncToolBase] = []

    # -- Always available (source-tree tools) --
    from harness.tools.edit import EditTool
    from harness.tools.findn import FindNTool
    from harness.tools.insight import InsightTool
    from harness.tools.listn import ListNTool
    from harness.tools.readn import ReadNTool
    from harness.tools.ripgrepn import RipgrepNTool
    from harness.tools.write import WriteTool

    tools.append(ReadNTool(self.acl))
    tools.append(ListNTool(self.acl))
    tools.append(FindNTool(self.acl))
    tools.append(RipgrepNTool(self.acl))
    tools.append(EditTool(self.acl))
    tools.append(WriteTool(self.acl))

    # -- Insight tool (persistent cross-run knowledge) --
    insight_dir = Path(harness.require_home_dir()) / "insight"
    tools.append(InsightTool(insight_dir))

    # Bash is always available but not scoped by ACL at the file level.
    from harness.tools.bash import BashTool

    tools.append(BashTool(self.acl))

    # -- Build-dir tools --
    from harness.tools.llvm_check_optim import CheckOptimTool
    from harness.tools.llvm_llc import CompileIrTool
    from harness.tools.llvm_lli import ExecuteIrTool
    from harness.tools.llvm_llubi import InterpretIrTool
    from harness.tools.llvm_opt import OptimizeIrTool

    build_dir = str(self.build_dir)
    tools.append(OptimizeIrTool(build_dir))
    tools.append(ExecuteIrTool(build_dir))
    tools.append(CompileIrTool(build_dir))
    tools.append(InterpretIrTool(build_dir))
    tools.append(CheckOptimTool(build_dir))

    # -- Alive2-based verification tools (require alive-tv) --
    from harness.tools.llvm_alive2 import VerifyIrTool
    from harness.tools.llvm_verify_optim import VerifyOptimTool

    tools.append(VerifyIrTool(self.alive_tv_path))
    tools.append(VerifyOptimTool(build_dir, str(self.alive_tv_path)))

    # -- Legacy llubi (independent of the LLVM build dir) --
    from harness.tools.llvm_llubi_legacy import InterpretIrLegacyTool

    tools.append(InterpretIrLegacyTool(str(self.llubi_legacy_path)))

    # -- Env-dependent tools (bench issue) --
    if self.fixenv is not None:
      from harness.tools.llvm_build import BuildTool
      from harness.tools.llvm_preview import PreviewTool
      from harness.tools.llvm_reset import ResetTool
      from harness.tools.llvm_test import TestTool

      # TODO: Decouple these tools from fixenv
      tools.append(BuildTool(self.fixenv))
      tools.append(TestTool(self.fixenv))
      tools.append(ResetTool(self.acl, self.fixenv))
      tools.append(PreviewTool(self.fixenv))

    # -- Debugger-dependent tools --
    if self._debugger is not None:
      from harness.tools.llvm_code import CodeTool
      from harness.tools.llvm_debug import DebugTool
      from harness.tools.llvm_docs import DocsTool
      from harness.tools.llvm_eval import EvalTool
      from harness.tools.llvm_langref import LangRefTool

      # TODO: Decouple these tools from debugger
      tools.append(LangRefTool(self.llvmcode))
      tools.append(CodeTool(self.llvmcode, self._debugger))
      tools.append(DocsTool(self.llvmcode, self._debugger))
      tools.append(DebugTool(self._debugger))
      tools.append(EvalTool(self._debugger))

    return tools

  def make_tool(self, name: str) -> FuncToolBase:
    """Instantiate a single tool by name.

    Raises :class:`KeyError` if the tool is not available.
    """
    tools = self.make_tools()
    for tool in tools:
      if tool.name() == name:
        return tool
    available = [t.name() for t in tools]
    raise KeyError(
      f"Tool {name!r} not available. Available tools: {', '.join(available)}"
    )
