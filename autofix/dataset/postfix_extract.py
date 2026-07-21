import argparse
import json
import os
import re
import subprocess

import requests
from unidiff import PatchSet

import autofix.dataset.hints as hints
import harness
import harness.llvm.intern.llvm as llvm_helper
from harness.llvm.intern.llvm_code import LlvmCode

harness.require_home_dir()

github_token = os.environ.get("LAB_GITHUB_TOKEN")
if not github_token:
  print(
    "Warning: LAB_GITHUB_TOKEN is not set. "
    "Using unauthenticated access (rate limit: 60 req/hour)."
  )

session = requests.Session()
headers = {
  "X-GitHub-Api-Version": "2022-11-28",
  "Accept": "application/vnd.github+json",
}
if github_token:
  headers["Authorization"] = f"Bearer {github_token}"
session.headers.update(headers)

subprocess.check_output(["llvm-extract", "--version"])

parser = argparse.ArgumentParser(description="Extract and process LLVM issue data.")
parser.add_argument("issue", type=str, help="The ID of the LLVM issue to process.")
parser.add_argument(
  "-f", "--force", action="store_true", help="Force override existing data."
)
args = parser.parse_args()

issue_id = args.issue
force = args.force

if force:
  print("Force override")

data_json_path = os.path.join(llvm_helper.dataset_dir, f"{issue_id}.json")
if not force and os.path.exists(data_json_path):
  print(f"Error: Item {issue_id}.json already exists (--force not set).")
  exit(1)

issue_url = f"https://api.github.com/repos/llvm/llvm-project/issues/{issue_id}"
print(f"Fetching {issue_url}")
issue = session.get(issue_url).json()
if (issue["state"] != "closed" or issue["state_reason"] != "completed") and not force:
  print("The issue/PR should be closed")
  exit(1)

knowledge_cutoff = issue["created_at"]
timeline = session.get(issue["timeline_url"] + "?per_page=100").json()
fix_commit = None

for event in timeline:
  if event["event"] == "closed":
    commit_id = event["commit_id"]
    if commit_id is not None:
      fix_commit = commit_id
      break
  if event["event"] == "referenced" and fix_commit is None:
    commit = event["commit_id"]
    if llvm_helper.is_valid_fix(commit):
      fix_commit = commit

if fix_commit is None:
  if force:
    fix_commit = llvm_helper.git_execute(["rev-parse", "origin/main"]).strip()
  else:
    print("Cannot find the fix commit")
    exit(0)

issue_type = "unknown"
for label in issue["labels"]:
  label_name = label["name"]
  if label_name == "miscompilation":
    issue_type = "miscompilation"
  if "crash" in label_name:
    issue_type = "crash"
  if "hang" in label_name:
    print("Hang issues are ignored for now.")
    exit(1)
  if label_name in [
    "invalid",
    "wontfix",
    "duplicate",
    "undefined behavior",
    "miscompilation:undef",
  ]:
    print("This issue is marked as invalid")
    exit(1)
  if label_name in (
    "backend",
    "llvm:codegen",
    "llvm:SelectionDAG",
  ) or label_name.startswith("llvm:Target/"):
    if issue_type == "miscompilation":
      issue_type = "backend-miscompilation"

base_commit = llvm_helper.git_execute(["rev-parse", fix_commit + "~"]).strip()
changed_files = llvm_helper.git_execute(
  ["show", "--name-only", "--format=", fix_commit]
).strip()
if "/AsmParser/" in changed_files or "/Bitcode/" in changed_files:
  print("This issue is marked as invalid")
  exit(0)

if issue_type == "miscompilation":
  has_backend = (
    "/Target/" in changed_files
    or "/CodeGen/" in changed_files
    or "/SelectionDAG/" in changed_files
  )
  has_midend = "/Transforms/" in changed_files or "/Analysis/" in changed_files
  if has_backend and not has_midend:
    issue_type = "backend-miscompilation"

# Component level
components = LlvmCode.infer_related_components(changed_files.split("\n"))
# Extract patch
patch = llvm_helper.git_execute(
  ["show", fix_commit, "--", "llvm/lib/*", "llvm/include/*"]
)
patchset = PatchSet(patch)
# Line level
bug_location_lineno = {}
for file in patchset:
  location = hints.get_line_loc(file)
  if len(location) != 0:
    bug_location_lineno[file.path] = location


# Function level

bug_location_funcname = {}
for file in patchset.modified_files:
  print(f"Parsing {file.path}")
  source_code = llvm_helper.git_execute(["show", f"{base_commit}:{file.path}"])
  modified_funcs_valid = hints.get_funcname_loc(file, source_code)
  if len(modified_funcs_valid) != 0:
    bug_location_funcname[file.path] = sorted(modified_funcs_valid)

# Extract tests
test_patchset = PatchSet(
  llvm_helper.git_execute(["show", fix_commit, "--", "llvm/test/*"])
)


def remove_target_suffix(path):
  targets = [
    "X86",
    "AArch64",
    "ARM",
    "Mips",
    "RISCV",
    "PowerPC",
    "LoongArch",
    "AMDGPU",
    "SystemZ",
    "Hexagon",
    "NVPTX",
  ]
  for target in targets:
    path = path.removesuffix("/" + target)
  return path


def _trim_to_target_dir(path: str) -> str:
  """For backend-miscompilation, keep the path up to the target directory,
  stripping sub-targets like msa/rvv while preserving the main target name.
  e.g. llvm/test/CodeGen/Mips/msa -> llvm/test/CodeGen/Mips
  """
  targets = [
    "X86",
    "AArch64",
    "ARM",
    "Mips",
    "RISCV",
    "PowerPC",
    "LoongArch",
    "AMDGPU",
    "SystemZ",
    "Hexagon",
    "NVPTX",
  ]
  parts = path.split("/")
  if len(parts) <= 3:
    return path
  if parts[-1] in targets:
    return path
  if len(parts) >= 4 and parts[-2] in targets:
    return "/".join(parts[:-1])
  return path


lit_test_dir = set()
for path in filter(lambda x: x.count("llvm/test/"), changed_files.split("\n")):
  d = os.path.dirname(path)
  if issue_type == "backend-miscompilation":
    d = _trim_to_target_dir(d)
  else:
    d = remove_target_suffix(d)
  lit_test_dir.add(d)


def _extract_triple_from_cmd(cmd: str) -> str | None:
  m = re.search(r"-mtriple[= ](\S+)", cmd)
  if m:
    return m.group(1)
  m = re.search(r"-march[= ](\S+)", cmd)
  if m:
    return m.group(1)
  return None


def _extract_target_triple(ir: str) -> str | None:
  m = re.search(r'target triple\s*=\s*"(.+?)"', ir)
  if m:
    return m.group(1)
  return None


tests = []
# FIXME: Run line extraction is fragile. It doesn't handle the cases that involve macros.
# FIXME: The comments in regression tests may leak information about the original issue.
runline_pattern = re.compile(r"; RUN: (.+)\| FileCheck")
testname_pattern = re.compile(r"define .+ @([.\w]+)\(")
for file in test_patchset:
  test_file = llvm_helper.git_execute(["show", f"{fix_commit}:{file.path}"])
  commands = []
  for match in re.findall(runline_pattern, test_file):
    commands.append(match.strip())

  if issue_type == "backend-miscompilation":
    ir_triple = _extract_target_triple(test_file)
    filtered = []
    for cmd in commands:
      triple = _extract_triple_from_cmd(cmd)
      if not triple:
        triple = ir_triple
        if triple:
          cmd += f" -mtriple={triple}"
      if triple and (
        triple.startswith("riscv64")
        or triple.startswith("aarch64")
        or triple.startswith("arm64")
      ):
        filtered.append(cmd)
    commands = filtered
    if not commands:
      continue
  if (
    issue_type not in ("miscompilation", "backend-miscompilation")
    and file.is_added_file
  ):
    print(file.path, "full")

    def is_valid_test_line(line: str):
      line = line.strip()
      if (
        line.startswith("; NOTE")
        or line.startswith("; RUN")
        or line.startswith("; CHECK")
      ):
        return False
      return True

    normalized_body = "\n".join(filter(is_valid_test_line, test_file.splitlines()))
    tests.append(
      {
        "file": file.path,
        "commands": commands,
        "tests": [{"test_name": "<module>", "test_body": normalized_body}],
      }
    )
    continue
  test_names = set()
  for hunk in file:
    matched = re.search(testname_pattern, hunk.section_header)
    if matched:
      test_names.add(matched.group(1))
    for line in hunk.target:
      for match in re.findall(testname_pattern, line):
        test_names.add(match.strip())
  print(file.path, test_names)
  subtests = []
  for test_name in test_names:
    try:
      test_body = subprocess.check_output(
        ["llvm-extract", f"--func={test_name}", "-S", "-"],
        input=test_file.encode(),
      ).decode()
      test_body = test_body.removeprefix(
        "; ModuleID = '<stdin>'\nsource_filename = \"<stdin>\"\n"
      ).removeprefix("\n")
      subtests.append(
        {
          "test_name": test_name,
          "test_body": test_body,
        }
      )
    except Exception:
      pass
  if len(subtests) != 0:
    tests.append({"file": file.path, "commands": commands, "tests": subtests})

# Extract full issue context
issue_comments = []
comments = session.get(issue["comments_url"]).json()
for comment in comments:
  comment_obj = {
    "author": comment["user"]["login"],
    "body": comment["body"],
  }
  if llvm_helper.is_valid_comment(comment_obj):
    issue_comments.append(comment_obj)
normalized_issue = {
  "title": issue["title"],
  "body": issue["body"],
  "author": issue["user"]["login"],
  "labels": list(map(lambda x: x["name"], issue["labels"])),
  "comments": issue_comments,
}

bug_func_count = 0
for item in bug_location_funcname.values():
  bug_func_count += len(item)
is_single_file_fix = (
  len(set(bug_location_funcname.keys()) | set(bug_location_lineno.keys())) == 1
)
is_single_func_fix = bug_func_count == 1

# Write to file
metadata = {
  "bug_id": issue_id,
  "issue_url": issue["html_url"],
  "bug_type": issue_type,
  "base_commit": base_commit,
  "knowledge_cutoff": knowledge_cutoff,
  "lit_test_dir": sorted(lit_test_dir),
  "hints": {
    "fix_commit": fix_commit,
    "components": sorted(components),
    "bug_location_lineno": bug_location_lineno,
    "bug_location_funcname": bug_location_funcname,
  },
  "patch": patch,
  "tests": tests,
  "issue": normalized_issue,
  "properties": {
    "is_single_file_fix": is_single_file_fix,
    "is_single_func_fix": is_single_func_fix,
    "difficulty": "easy"
    if is_single_file_fix and is_single_func_fix
    else "medium"
    if is_single_file_fix
    else "hard",
  },
}
print(json.dumps(metadata, indent=2))
with open(data_json_path, "w") as f:
  json.dump(metadata, f, indent=2, sort_keys=True)
print(f"Saved to {data_json_path}")
