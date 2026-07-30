import json
import os
import subprocess
import tempfile
from multiprocessing import Pool
from typing import Dict, List

from harness.utils import cmdline

_OPT_CRASH_INDICATORS = [
  "LLVM ERROR",
  "compilation aborted",
  "Stack dump:",
  "Broken module found",
  "does not dominate all uses",
  "PLEASE submit a bug report",
]


llvm_dir = os.environ["LAB_LLVM_DIR"]
__llvm_build_dir = os.environ["LAB_LLVM_BUILD_DIR"]
llvm_alive_tv = os.environ["LAB_LLVM_ALIVE_TV"]
llvm_llubi_legacy = os.environ["LAB_LLVM_LLUBI_LEGACY"]
llvm_backend_tv = os.environ.get(
  "LAB_LLVM_BACKEND_TV", "/llvm-harness-deps/backend-tv/backend-tv/build/backend-tv"
)
dataset_dir = os.environ["LAB_DATASET_DIR"]
if "--quiet" not in subprocess.run(
  ["ninja", "--help"], capture_output=True
).stderr.decode("utf-8"):
  raise RuntimeError("Please update ninja to version 1.11.0 or later")


def load_benchmark_issue(issue_id: str) -> dict:
  """Load a benchmark issue JSON from the dataset directory."""
  with open(os.path.join(dataset_dir, f"{issue_id}.json")) as f:
    return json.load(f)


def _decode_output(output):
  if output is None:
    return ""
  return output.decode()


def is_opt_crash(msg: str) -> bool:
  return any(indicator in msg for indicator in _OPT_CRASH_INDICATORS)


def git_execute(args):
  return subprocess.check_output(
    ["git", "-C", llvm_dir] + args, cwd=llvm_dir, stderr=subprocess.DEVNULL
  ).decode("utf-8")


def reset(commit):
  git_execute(["restore", "--staged", "."])
  git_execute(["clean", "-fdx"])
  git_execute(["checkout", "."])
  git_execute(["checkout", commit])


def pull_latest():
  reset("main")
  git_execute(["pull", "origin", "main"])


def build(max_build_jobs: int, additional_cmake_args=[]):
  os.makedirs(__llvm_build_dir, exist_ok=True)
  log = ""
  # TODO: we can set CCACHE_NOHASHDIR to allow ccache to reuse objects built in different directories.
  # Be careful about the debug prefix mapping though.
  try:
    log += subprocess.check_output(
      [
        "cmake",
        "-S",
        llvm_dir + "/llvm",
        "-G",
        "Ninja",
        "-DBUILD_SHARED_LIBS=ON",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DLLVM_ENABLE_ASSERTIONS=ON",
        "-DLLVM_ABI_BREAKING_CHECKS=WITH_ASSERTS",
        "-DLLVM_ENABLE_WARNINGS=OFF",
        "-DLLVM_APPEND_VC_REV=OFF",
        "-DLLVM_TARGETS_TO_BUILD='X86;RISCV;AArch64;SystemZ;Hexagon;PowerPC;NVPTX;'",
        "-DLLVM_PARALLEL_LINK_JOBS=4",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
      ]
      + additional_cmake_args,
      stderr=subprocess.STDOUT,
      cwd=__llvm_build_dir,
    ).decode()
    pos = log.find("Build files have been written to")
    if pos != -1:
      pos = log.find("\n", pos)
      if pos != -1:
        log = log[pos + 1 :]
    log += subprocess.check_output(
      ["cmake", "--build", ".", "-j", str(max_build_jobs), "--", "--quiet"],
      stderr=subprocess.STDOUT,
      cwd=__llvm_build_dir,
    ).decode()
    return (True, log)
  except subprocess.CalledProcessError as e:
    return (False, log + "\n" + _decode_output(e.output))


def is_valid_comment(comment):
  if comment["author"] == "llvmbot":
    return False
  if comment["body"].startswith("/cherry-pick"):
    return False
  return True


def apply_patch(patch: str):
  try:
    out = subprocess.check_output(
      ["git", "-C", llvm_dir, "apply"],
      cwd=llvm_dir,
      stderr=subprocess.STDOUT,
      input=patch.encode(),
    ).decode("utf-8")
    return (True, out)
  except subprocess.CalledProcessError as e:
    return (False, str(e) + "\n" + _decode_output(e.output))


def filter_out_unsupported_feats(src: str):
  src = src.replace(" noalias ", " ")
  src = src.replace(" nofree ", " ")
  return src


def alive2_check(src: str, tgt: str, additional_args: str, repro: bool):
  try:
    with tempfile.NamedTemporaryFile() as src_file:
      with tempfile.NamedTemporaryFile() as tgt_file:
        src = filter_out_unsupported_feats(src)
        tgt = filter_out_unsupported_feats(tgt)
        src_file.write(src.encode())
        tgt_file.write(tgt.encode())
        src_file.flush()
        tgt_file.flush()

        args = [
          llvm_alive_tv,
          src_file.name,
          tgt_file.name,
        ]
        if additional_args:
          args += additional_args.strip().split(" ")

        out = subprocess.check_output(args, stderr=subprocess.STDOUT).decode()
        # NOTE: !success doesn't imply reproducible.
        # Affected issues: 136430/140444
        success = (
          "0 incorrect transformations" in out
          and "0 failed-to-prove transformations" in out
          and "0 Alive2 errors" in out
        )
        failure = (
          "0 incorrect transformations" not in out
          and "0 failed-to-prove transformations" in out
          and "0 Alive2 errors" in out
        )
        return (failure if repro else success, {"src": src, "tgt": tgt, "log": out})
  except subprocess.CalledProcessError as e:
    return (False, str(e) + "\n" + _decode_output(e.output))


def backend_tv_check(
  ir: str, asm: str, backend: str, additional_args: str, repro: bool
):
  try:
    with tempfile.NamedTemporaryFile(suffix=".ll") as ir_file:
      with tempfile.NamedTemporaryFile(suffix=".s") as asm_file:
        ir_file.write(ir.encode())
        asm_file.write(asm.encode())
        ir_file.flush()
        asm_file.flush()

        args = [
          llvm_backend_tv,
          "--asm-input",
          asm_file.name,
          "--backend",
          backend,
          "--disable-undef-input",
        ]
        if additional_args:
          args += additional_args.strip().split(" ")
        args.append(ir_file.name)

        out = subprocess.check_output(args, stderr=subprocess.STDOUT).decode()
        success = (
          "0 incorrect transformations" in out
          and "0 failed-to-prove transformations" in out
          and "0 Alive2 errors" in out
        )
        failure = (
          "0 incorrect transformations" not in out
          and "0 failed-to-prove transformations" in out
          and "0 Alive2 errors" in out
        )
        return (failure if repro else success, {"log": out})
  except subprocess.CalledProcessError as e:
    return (False, str(e) + "\n" + _decode_output(e.output))


def copy_triple(input: str, out: bytes):
  triple_pattern = "target triple ="
  if triple_pattern in input:
    return input
  ref_out = out.decode()
  if triple_pattern in ref_out:
    triple_pos = ref_out.find(triple_pattern)
    triple_line = ref_out[triple_pos : ref_out.find("\n", triple_pos) + 1]
    return triple_line + input
  return input


def copy_datalayout(input: str, out: bytes):
  datalayout_pattern = "target datalayout ="
  if datalayout_pattern in input:
    return input
  ref_out = out.decode()
  if datalayout_pattern in ref_out:
    datalayout_pos = ref_out.find(datalayout_pattern)
    datalayout_line = ref_out[datalayout_pos : ref_out.find("\n", datalayout_pos) + 1]
    return datalayout_line + input
  return input


def _extract_backend_target(args: str) -> str:
  """Extract the target backend name from an llc command string.

  Parses ``-mtriple`` (e.g. ``aarch64-unknown-linux-gnu`` → ``aarch64``)
  or ``-march`` (e.g. ``-march=aarch64`` → ``aarch64``). Normalses aliases
  (``arm64`` → ``aarch64``) for ``backend-tv`` compatibility. Returns an
  empty string when no target is found (backend-tv will use its default).
  """
  import re

  m = re.search(r"-mtriple[= ](\S+)", args)
  if m:
    arch = m.group(1).split("-")[0]
  else:
    m = re.search(r"-march[= ](\S+)", args)
    if m:
      arch = m.group(1)
    else:
      return ""

  if arch in ("arm64", "arm64e"):
    arch = "aarch64"
  return arch


def verify_dispatch(
  repro: bool,
  input: str,
  args: str,
  type: str,
  additional_args: str,
):
  if type == "backend-miscompilation":
    tool_name = "llc"
    timeout = 60.0
  else:
    tool_name = "opt"
    timeout = 600.0 if type == "crash" else 10.0

  tool_path = os.path.join(__llvm_build_dir, "bin", tool_name)

  args_list = list(
    filter(
      lambda x: x != "",
      args.replace("< ", " ")
      .replace("%s", "-")
      .replace("2>&1", "")
      .replace("'", "")
      .replace('"', "")
      .replace(tool_name, tool_path, 1)
      .strip()
      .split(" "),
    )
  )
  try:
    out = subprocess.run(
      args_list,
      input=input.encode(),
      timeout=timeout,
      check=True,
      capture_output=True,
    )
    if type == "miscompilation":
      output = out.stdout
      new_input = copy_triple(input, output)
      new_input = copy_datalayout(new_input, output)
      alive2_args = "--disable-undef-input --smt-to=60000"
      if additional_args:
        alive2_args += " " + additional_args
      res, log = alive2_check(new_input, output.decode(), alive2_args, repro)
      if isinstance(log, str):
        log = _decode_output(out.stderr) + "\n" + log
      else:
        log["opt_stderr"] = _decode_output(out.stderr)
      return (res, log)
    if type == "backend-miscompilation":
      backend = _extract_backend_target(args)
      asm = out.stdout.decode()
      backend_tv_args = "--smt-to=60000"
      if additional_args:
        backend_tv_args += " " + additional_args
      res, log = backend_tv_check(input, asm, backend, backend_tv_args, repro)
      if isinstance(log, str):
        log = _decode_output(out.stderr) + "\n" + log
      else:
        log["llc_stderr"] = _decode_output(out.stderr)
      return (res, log)
    return (not repro, "success\n" + _decode_output(out.stderr))
  except subprocess.CalledProcessError as e:
    return (
      repro and type == "crash",
      str(e) + "\n" + _decode_output(e.output) + "\n" + _decode_output(e.stderr),
    )
  except subprocess.TimeoutExpired as e:
    return (
      repro and type == "hang",
      str(e) + "\n" + _decode_output(e.output) + "\n" + _decode_output(e.stderr),
    )


def verify_test_group(repro: bool, input, type: str):
  test_res = []
  overall_test_res = not repro
  for test in input:
    file = test["file"]
    commands = test["commands"]
    tests = test["tests"]
    for subtest in tests:
      name = subtest["test_name"]
      body = subtest["test_body"]
      for args in commands:
        res, log = verify_dispatch(
          repro,
          body,
          args,
          type,
          subtest.get("additional_args"),
        )
        test_res.append(
          {
            "file": file,
            "args": args,
            "name": name,
            "body": body,
            "result": res,
            "log": log,
          }
        )
        if repro:
          overall_test_res = overall_test_res or res
        else:
          overall_test_res = overall_test_res and res
  return (overall_test_res, test_res)


def verify_lit(
  test_commit, dirs, max_test_jobs, test_commit_checkout_changed_files_only
):
  if not dirs:
    # Ad-hoc reproducers may have no scoped lit dir; skip the lit step rather
    # than crash. Aggressive callers (e.g. post_validate) supply Transforms +
    # Analysis explicitly, so this only short-circuits the genuinely-empty case.
    return (True, "no lit dirs configured; skipping lit regression")
  try:
    # In some edge cases, we cannot find a suitable test commit that is buildable and green.
    # We only checkout the changed files instead.
    if test_commit_checkout_changed_files_only:
      files = (
        git_execute(["show", "--name-only", "--format=", test_commit, "--"] + dirs)
        .strip()
        .splitlines()
      )
      for file in files:
        git_execute(["checkout", test_commit, file])
    else:
      git_execute(["checkout", test_commit, "llvm/test"])
    test_dirs = [os.path.join(llvm_dir, x) for x in dirs]
    # TODO: use --order=random/--max-tests/--max-time for test sampling
    out = cmdline.check_output(
      " ".join(
        [
          os.path.join(__llvm_build_dir, "bin/llvm-lit"),
          "--no-progress-bar",
          "-j",
          str(max_test_jobs),
          "--max-failures",
          "1",
          "--order",
          "lexical",
          "-sv",
        ]
        + test_dirs
      ),
      timeout=300,
    ).decode()
    return (True, out)
  except subprocess.CalledProcessError as e:
    return (False, str(e) + "\n" + _decode_output(e.output))
  except subprocess.TimeoutExpired as e:
    return (False, str(e) + "\n" + _decode_output(e.output))


# TODO: Use https://github.com/llvm/llvm-project/blob/main/.ci/generate_test_report_lib.py for pretty test result reporting.
def get_first_failed_test(test_result):
  for res in test_result:
    if not res["result"]:
      return res
  return None


def is_valid_fix(commit):
  if commit is None:
    return False
  try:
    branches = git_execute(["branch", "--contains", commit])
    if "main\n" not in branches:
      return False
    changed_files = (
      subprocess.check_output(
        [
          "git",
          "-C",
          llvm_dir,
          "show",
          "--name-only",
          "--format=",
          commit,
        ],
        stderr=subprocess.DEVNULL,
      )
      .decode()
      .strip()
    )
    if "llvm/test/" in changed_files and (
      "llvm/lib/" in changed_files or "llvm/include/" in changed_files
    ):
      return True
  except subprocess.CalledProcessError:
    pass
  return False


def pretty_render_log(log) -> str:
  if isinstance(log, str):
    return log
  if isinstance(log, dict):
    pretty_log = ""
    for key, value in log.items():
      pretty_log += f"--- {key} ---\n{pretty_render_log(value)}\n\n"
    return pretty_log
  return str(log)


def set_llvm_build_dir(new_dir: str):
  global __llvm_build_dir
  __llvm_build_dir = new_dir


def get_llvm_build_dir() -> str:
  return __llvm_build_dir


def compute_O3_output(file: str) -> str:
  opt_path = os.path.join(get_llvm_build_dir(), "bin", "opt")
  try:
    output = subprocess.check_output(
      [opt_path, "-O3", file, "-S"], timeout=60.0, stderr=subprocess.DEVNULL
    )
    res = output.decode("utf-8")
    return file, res
  except Exception:
    return file, None


def batch_compute_O3_output(tasks: List[str], jobs: int) -> Dict[str, str]:
  hashes = dict()
  with Pool(processes=jobs) as pool:
    for res in pool.imap_unordered(compute_O3_output, tasks):
      file, hash = res
      if not hash:
        continue
      hashes[file] = hash
  return hashes
