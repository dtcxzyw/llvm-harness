import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from typing import Optional

import harness.llvm.intern.llvm as llvm_ops
from harness.llvm.issue import IssueCard


class TimeCompensationGuard:
  def __init__(self, environment):
    self.environment = environment

  def __enter__(self):
    self.start_time = time.time()
    self.environment.interaction_time_compensation_enter += 1

  def __exit__(self, exception_type, exception_value, exception_traceback):
    self.environment.interaction_time_compensation_enter -= 1
    if self.environment.interaction_time_compensation_enter == 0:
      self.environment.interaction_time_compensation += time.time() - self.start_time


class FixEnv:
  def __init__(
    self,
    card: IssueCard,
    *,
    max_build_jobs=None,
    max_test_jobs=None,
    use_entire_regression_test_suite=False,
    additional_cmake_args=[],
    test_commit_checkout_changed_files_only=False,
    reference_patch: str | None = None,
  ):
    self.card = card
    self.base_commit = card.base_commit or (
      llvm_ops.git_execute(["rev-parse", "HEAD"]).strip()
    )
    self.bug_type = card.bug_type
    self.test_commit = card.test_commit or self.base_commit
    self.test_commit_checkout_changed_files_only = (
      test_commit_checkout_changed_files_only
    )
    self.reference_patch = reference_patch
    self.interaction_time_compensation = 0.0
    self.interaction_time_compensation_enter = 0
    self.build_count = 0
    self.build_failure_count = 0
    self.fast_check_count = 0
    self.full_check_count = 0
    self.fast_check_pass = False
    self.full_check_pass = False
    if max_build_jobs is None:
      self.max_build_jobs = os.cpu_count()
    else:
      self.max_build_jobs = max_build_jobs
    if max_test_jobs is None:
      self.max_test_jobs = max_build_jobs
    else:
      self.max_test_jobs = max_test_jobs
    self.additional_cmake_args = additional_cmake_args
    self.use_entire_regression_test_suite = use_entire_regression_test_suite
    self.start_time = time.time()
    self._reset_or_pull_reset()

  def _reset_or_pull_reset(self):
    try:
      self.reset()
    except Exception:
      # Sync and retry once.
      llvm_ops.pull_latest()
      self.reset()

  def reset(self, *, files: list[str] | None = None):
    with TimeCompensationGuard(self):
      if files is not None:
        for f in files:
          llvm_ops.git_execute(["checkout", self.base_commit, f])
      else:
        llvm_ops.reset(self.base_commit)

  def verify_head(self):
    head = llvm_ops.git_execute(["rev-parse", "HEAD"]).strip()
    if head != self.base_commit:
      raise RuntimeError("invalid HEAD")

  def build(self):
    with TimeCompensationGuard(self):
      self.build_count += 1
      self.verify_head()
      res, log = llvm_ops.build(self.max_build_jobs, self.additional_cmake_args)
      if not res:
        self.build_failure_count += 1
      return res, log

  def dump(self, log=None):
    wall_time = time.time() - self.start_time - self.interaction_time_compensation
    self.verify_head()
    patch = self.dump_patch()
    return {
      "wall_time": wall_time,
      "build_count": self.build_count,
      "build_failure_count": self.build_failure_count,
      "fast_check_count": self.fast_check_count,
      "full_check_count": self.full_check_count,
      "fast_check_pass": self.fast_check_pass,
      "full_check_pass": self.full_check_pass,
      "patch": patch,
      "log": log,
    }

  def dump_patch(self):
    return llvm_ops.git_execute(
      ["diff", self.base_commit, "--", "llvm/lib/*", "llvm/include/*"]
    )

  def check_fast(self):
    """
    Run the reproducer test(s) only.
    """
    with TimeCompensationGuard(self):
      self.fast_check_count += 1
      res, reason = self.build()
      if not res:
        return (False, reason)
      res, log = llvm_ops.verify_test_group(
        repro=False,
        input=[asdict(r) for r in self.card.reproducers],
        type=self.bug_type,
      )
      if not res:
        return (False, log)
      self.fast_check_pass = True
      return (True, log)

  def check_midend(self):
    """
    Run the middle-end regression (Transforms/ and Analysis/) tests.
    """
    with TimeCompensationGuard(self):
      self.full_check_count += 1
      res, reason = self.build()
      if not res:
        return (False, reason)
      return llvm_ops.verify_lit(
        test_commit=self.test_commit,
        dirs=self._lit_dirs(),
        max_test_jobs=self.max_build_jobs,
        test_commit_checkout_changed_files_only=self.test_commit_checkout_changed_files_only,
      )

  def _lit_dirs(self):
    if self.use_entire_regression_test_suite:
      if self.bug_type.startswith("backend-"):
        return ["llvm/test/CodeGen"]
      return ["llvm/test/Transforms", "llvm/test/Analysis"]
    return self.card.lit_test_dir

  # Please disable ASLR and compile LLVM with -DLLVM_ABI_BREAKING_CHECKS=FORCE_OFF
  # to ensure deterministic output.
  def check_regression_diff(self, seeds_dir: Optional[str] = None):
    """
    Run the regression tests and check the output bitcode with llvm-diff.
    """
    if self.reference_patch is None:
      raise RuntimeError("No reference patch available for regression diff check.")
    with open("/proc/sys/kernel/randomize_va_space", "r") as f:
      if int(f.read().strip()) != 0:
        print("Warning: ASLR is enabled. Please disable it for deterministic output.")
        return (True, "ASLR is enabled")
    if "-DLLVM_ABI_BREAKING_CHECKS=FORCE_OFF" not in self.additional_cmake_args:
      print(
        "Warning: Please compile LLVM with -DLLVM_ABI_BREAKING_CHECKS=FORCE_OFF for deterministic output."
      )
      return (True, "LLVM_ABI_BREAKING_CHECKS is enabled")
    with TimeCompensationGuard(self):
      self.full_check_count += 1
      patch = self.dump_patch()

      self.reset()
      llvm_ops.apply_patch(self.reference_patch)
      res, reason = self.build()
      if not res:
        self.reset()
        llvm_ops.apply_patch(patch)
        return (False, reason)

      tasks = []
      if seeds_dir is None:
        seeds_dir = os.path.join(llvm_ops.llvm_dir, "llvm", "test")
      for r, _, fs in os.walk(seeds_dir):
        for f in fs:
          if f.endswith(".ll"):
            src_path = os.path.join(r, f)
            tasks.append(src_path)

      gold_result = llvm_ops.batch_compute_O3_output(tasks, self.max_test_jobs)

      self.reset()
      llvm_ops.apply_patch(patch)
      res, reason = self.build()
      if not res:
        return (False, reason)
      patch_result = llvm_ops.batch_compute_O3_output(
        list(gold_result.keys()), self.max_test_jobs
      )

      valid_count = 0
      for file, res in patch_result.items():
        if gold_result[file] == res:
          valid_count += 1
        else:
          # use llvm-diff for structural comparison
          with (
            tempfile.NamedTemporaryFile("w") as f_gold,
            tempfile.NamedTemporaryFile("w") as f_patch,
          ):
            f_gold.write(gold_result[file])
            f_gold.flush()
            f_patch.write(res)
            f_patch.flush()
            try:
              subprocess.check_call(
                [
                  os.path.join(llvm_ops.get_llvm_build_dir(), "bin", "llvm-diff"),
                  f_gold.name,
                  f_patch.name,
                ],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
              )
              valid_count += 1
            except subprocess.CalledProcessError:
              pass
      if valid_count == len(gold_result):
        return (True, "success")
      return (False, f"failure ({valid_count}/{len(gold_result)})")

  def check_pass(self):
    """
    Run the pass-specific regression tests.
    """
    with TimeCompensationGuard(self):
      self.full_check_count += 1
      res, reason = self.build()
      if not res:
        return (False, reason)
      res, log = llvm_ops.verify_test_group(
        repro=False,
        input=[asdict(r) for r in self.card.reproducers],
        type=self.bug_type,
      )
      if not res:
        return (False, log)
      self.fast_check_pass = True
      # If use_entire_regression_test_suite is True, run the entire regression test suite.
      # By default, only run the tests in the specified lit_test_dir to save time.
      res, log = llvm_ops.verify_lit(
        test_commit=self.test_commit,
        dirs=self._lit_dirs(),
        max_test_jobs=self.max_build_jobs,
        test_commit_checkout_changed_files_only=self.test_commit_checkout_changed_files_only,
      )
      if not res:
        return (False, log)
      self.full_check_pass = True
      return (True, log)

  def get_bug_type(self):
    return self.bug_type

  def get_base_commit(self):
    return self.base_commit

  def get_reference_patch(self):
    return self.reference_patch

  def get_issue_title(self):
    issue = self.card.issue
    return issue["title"] if issue else None

  def get_issue_labels(self):
    issue = self.card.issue
    return issue.get("labels", []) if issue else []
