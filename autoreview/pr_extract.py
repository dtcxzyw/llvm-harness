import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from unidiff import PatchSet

from harness.llvm.intern import llvm as llvm_ops
from harness.llvm.intern.llvm_code import LlvmCode


@dataclass
class PRInfo:
  pr_id: int
  pr_url: str = ""
  title: str = ""
  author: str = ""
  base_commit: str = ""
  fix_commit: str = ""
  patch: str = ""
  components: list[str] = field(default_factory=list)
  state: str = ""
  knowledge_cutoff: str = ""
  description: str = ""
  tests: list[dict] = field(default_factory=list)
  labels: list[str] = field(default_factory=list)
  comments: list[dict] = field(default_factory=list)
  patch_location_lineno: dict = field(default_factory=dict)
  patch_location_funcname: dict = field(default_factory=dict)


def _append_query(url: str, **params) -> str:
  parsed = urlparse(url)
  merged = dict(parse_qsl(parsed.query, keep_blank_values=True))
  merged.update({k: str(v) for k, v in params.items()})
  return urlunparse(parsed._replace(query=urlencode(merged)))


def _github_headers(accept: str) -> dict[str, str]:
  token = os.environ.get("LAB_GITHUB_TOKEN")
  headers = {
    "X-GitHub-Api-Version": "2022-11-28",
    "Accept": accept,
    "User-Agent": "llvm-autoreview",
  }
  if token:
    headers["Authorization"] = f"Bearer {token}"
  return headers


def _github_get(url: str, *, accept: str) -> tuple[bytes, object]:
  req = Request(url, headers=_github_headers(accept))
  try:
    with urlopen(req, timeout=60) as resp:
      return resp.read(), resp.headers
  except HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    raise RuntimeError(f"GitHub request failed: {e.code} {e.reason}\n{body}") from e
  except URLError as e:
    raise RuntimeError(f"GitHub request failed: {e}") from e


def _github_get_json(url: str) -> dict:
  payload, _ = _github_get(url, accept="application/vnd.github+json")
  return json.loads(payload.decode("utf-8"))


def _github_get_text(url: str, *, accept: str) -> str:
  payload, _ = _github_get(url, accept=accept)
  return payload.decode("utf-8", errors="replace")


def _github_paginated_json(url: str) -> list[dict]:
  page = 1
  items = []
  while True:
    batch = _github_get_json(_append_query(url, per_page=100, page=page))
    if not isinstance(batch, list):
      raise RuntimeError(
        f"Expected list response from GitHub for {url}, got {type(batch)}"
      )
    items.extend(batch)
    if len(batch) < 100:
      break
    page += 1
  return items


def _cache_root() -> Path:
  return Path(__file__).resolve().parent / "dataset"


def _cache_path(pr_id: int, state: str) -> Path:
  return _cache_root() / state / f"{pr_id}.json"


def load_cached_pr_info(pr_id: int) -> Optional[PRInfo]:
  for state in ["closed", "open"]:
    path = _cache_path(pr_id, state)
    if not path.exists():
      continue
    with path.open("r", encoding="utf-8") as fin:
      return PRInfo(**json.load(fin))
  return None


def save_pr_info(pr_info: PRInfo) -> Path:
  state_dir = _cache_root() / pr_info.state
  state_dir.mkdir(parents=True, exist_ok=True)
  path = state_dir / f"{pr_info.pr_id}.json"
  with path.open("w", encoding="utf-8") as fout:
    json.dump(asdict(pr_info), fout, indent=2)
  return path


def filter_patch_exclude_tests(full_patch: str) -> str:
  try:
    patchset = PatchSet(full_patch)
  except Exception:
    return full_patch
  filtered = [item for item in patchset if not item.path.startswith("llvm/test/")]
  return "".join(str(item) for item in filtered)


def _join_continuation_lines(text: str) -> str:
  """Join lines ending with backslash (line continuations) in LLVM test files.

  Handles two patterns found in LLVM RUN lines:
    1. ``; RUN: cmd \\`` followed by a non-``; RUN:`` continuation line
    2. ``; RUN: cmd \\`` followed by ``; RUN: continuation``
  In both cases the continuation is appended (without the leading
  ``; RUN:`` prefix, if present) to form a single logical line.
  """
  lines = text.splitlines()
  result: list[str] = []
  i = 0
  while i < len(lines):
    line = lines[i]
    if line.rstrip().endswith("\\"):
      parts = [line.rstrip()[:-1]]  # strip trailing backslash
      i += 1
      while i < len(lines):
        next_line = lines[i]
        if next_line.rstrip().endswith("\\"):
          stripped = next_line.rstrip()[:-1]
          # Remove optional leading "; RUN:" on continuation lines
          stripped = re.sub(r"^;\s*RUN:\s*", "", stripped)
          parts.append(stripped)
          i += 1
        else:
          stripped = next_line
          stripped = re.sub(r"^;\s*RUN:\s*", "", stripped)
          parts.append(stripped)
          i += 1
          break
      result.append("".join(parts))
    else:
      result.append(line)
      i += 1
  return "\n".join(result)


def _extract_run_commands(text: str) -> list[str]:
  """Extract RUN-line commands from an LLVM test file.

  1. Joins continuation lines (trailing backslash).
  2. Matches ``; RUN: <command>`` broadly (no FileCheck requirement).
  3. Truncates each command at the first ``| FileCheck`` (case-insensitive)
     to keep only the tool invocation before the check, matching the
     behaviour of the original regex while supporting more variants.
  """
  joined = _join_continuation_lines(text)
  raw_commands = re.findall(r";\s*RUN:\s*(.+)", joined)
  commands: list[str] = []
  for cmd in raw_commands:
    truncated = re.split(r"\|\s*[Ff]ile[Cc]heck", cmd)[0].strip()
    if truncated:
      commands.append(truncated)
  return commands


def extract_tests_from_patch(full_patch: str) -> list[dict]:
  tests = []
  try:
    patchset = PatchSet(full_patch)
  except Exception:
    return tests

  if not shutil.which("llvm-extract"):
    raise RuntimeError("The `llvm-extract` command is not found.")

  testname_pattern = re.compile(r"define .+ @([.\w]+)\(")
  llvm_dir = os.environ.get("LAB_LLVM_DIR")
  if not llvm_dir:
    raise RuntimeError("The `LAB_LLVM_DIR` environment variable is not set.")

  for file in patchset:
    if not file.path.startswith("llvm/test/") or file.is_removed_file:
      continue

    test_file_path = os.path.join(llvm_dir, file.path)
    try:
      test_file = Path(test_file_path).read_text(encoding="utf-8")
    except Exception:
      continue

    commands = _extract_run_commands(test_file)
    if not commands:
      print(f"WARNING: No RUN lines extracted from {file.path}")
    test_names = set()

    if file.is_added_file:
      for match in re.findall(testname_pattern, test_file):
        test_names.add(match.strip())
    else:
      for hunk in file:
        matched = re.search(testname_pattern, hunk.section_header)
        if matched:
          test_names.add(matched.group(1))
        for line in hunk.target:
          for match in re.findall(testname_pattern, line):
            test_names.add(match.strip())

    subtests = []
    for test_name in sorted(test_names):
      try:
        test_body = subprocess.check_output(
          ["llvm-extract", f"--func={test_name}", "-S", "-"],
          input=test_file.encode("utf-8"),
        ).decode("utf-8", errors="replace")
      except Exception:
        continue
      test_body = test_body.removeprefix(
        "; ModuleID = '<stdin>'\nsource_filename = \"<stdin>\"\n"
      ).removeprefix("\n")
      subtests.append({"test_name": test_name, "test_body": test_body})

    if not subtests:

      def is_valid_test_line(line: str) -> bool:
        stripped = line.strip()
        return not (
          stripped.startswith("; NOTE")
          or stripped.startswith("; RUN")
          or stripped.startswith("; CHECK")
        )

      normalized = "\n".join(filter(is_valid_test_line, test_file.splitlines()))
      if normalized.strip():
        tests.append(
          {
            "file": file.path,
            "commands": commands,
            "tests": [{"test_name": "<module>", "test_body": normalized}],
          }
        )
    else:
      tests.append({"file": file.path, "commands": commands, "tests": subtests})

  return tests


def fetch_pr_info(pr_id: int, *, refresh: bool = False) -> PRInfo:
  if not refresh:
    cached = load_cached_pr_info(pr_id)
    if cached is not None:
      return cached

  pr_api = f"https://api.github.com/repos/llvm/llvm-project/pulls/{pr_id}"
  pr = _github_get_json(pr_api)

  state = pr.get("state", "open")
  files = _github_paginated_json(pr["url"] + "/files")
  changed_files = [item["filename"] for item in files]
  changed_files_str = "\n".join(changed_files)
  if "/AsmParser/" in changed_files_str or "/Bitcode/" in changed_files_str:
    raise RuntimeError(
      "PR contains AsmParser or Bitcode changes, which are not supported."
    )

  llvm_files = [
    path for path in changed_files if path.startswith(("llvm/lib/", "llvm/include/"))
  ]
  components = sorted(LlvmCode.infer_related_components(llvm_files))
  if not components:
    raise RuntimeError("PR does not modify supported LLVM lib/include files.")

  patch = _github_get_text(pr_api, accept="application/vnd.github.v3.diff")
  base_commit = pr["base"]["sha"]
  fix_commit = pr["head"]["sha"]
  labels = [label["name"] for label in pr.get("labels", [])]

  comments = []
  for comment in _github_paginated_json(pr["comments_url"]):
    item = {"author": comment["user"]["login"], "body": comment["body"]}
    if llvm_ops.is_valid_comment(item):
      comments.append(item)

  print(f"Extracting tests from PR #{pr_id} on base commit {base_commit} ...")
  try:
    llvm_ops.reset(base_commit)
  except Exception as e:
    print(f"Failed to reset to the PR base commit: {e}")
    print("Syncing main and retrying ...")
    llvm_ops.pull_latest()
    llvm_ops.reset(base_commit)

  ok, log = llvm_ops.apply_patch(patch)
  if not ok:
    llvm_ops.reset("main")
    raise RuntimeError(f"Failed to apply PR patch for extraction:\n{log}")

  try:
    tests = extract_tests_from_patch(patch)
  finally:
    llvm_ops.reset("main")

  if not tests:
    raise RuntimeError("No LLVM tests were extracted from this PR.")

  pr_info = PRInfo(
    pr_id=pr_id,
    pr_url=pr.get("html_url", ""),
    title=pr.get("title", ""),
    author=pr.get("user", {}).get("login", ""),
    base_commit=base_commit,
    fix_commit=fix_commit,
    patch=filter_patch_exclude_tests(patch),
    components=components,
    state=state,
    knowledge_cutoff=pr.get("created_at", ""),
    description=pr.get("body") or "",
    tests=tests,
    labels=labels,
    comments=comments,
  )
  cache_path = save_pr_info(pr_info)
  print(f"Cached PR metadata to {cache_path}.")
  return pr_info
