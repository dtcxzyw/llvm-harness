import os
import subprocess
import time
from pathlib import Path

import requests
import tqdm

import harness.llvm.intern.llvm as llvm_helper

github_token = os.environ["LAB_GITHUB_TOKEN"]
cache_dir = os.environ["LAB_ISSUE_CACHE"]
postfix_extract = os.path.join(os.path.dirname(__file__), "postfix_extract.py")
session = requests.Session()
session.headers.update(
  {
    "X-GitHub-Api-Version": "2022-11-28",
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github+json",
  }
)

issue_id_begin = 76663  # Since 2024-01-01
issue_id_end = 183047

# Labels that mark an issue as always-skip (invalid / non-LLVM).
_ALWAYS_SKIP_LABELS = {
  "invalid",
  "wontfix",
  "duplicate",
  "undefined behavior",
  "miscompilation:undef",
}

# Labels that indicate a non-LLVM / non-middle-end area — skip regardless.
_NON_LLVM_PREFIXES = (
  "clang:",
  "clangd",
  "clang-tidy",
  "clang-format",
  "mlir",
  "tools:",
  "flang:",
  "lld:",
  "lldb",
  "tablegen",
  "polly",
  "PGO",
)

# Labels that indicate a backend bug.  These are *kept* when the issue is also a
# crash (→ backend-crash), but are otherwise skipped.
_BACKEND_LABELS = {
  "backend",
  "llvm:SelectionDAG",
  "llvm:globalisel",
  "llvm:regalloc",
  "llvm:codegen",
}

# NOTE: backend-miscompilation issues are currently extracted manually and
# are not covered by this automatic bulk-extraction script.


def wait(progress):
  try:
    rate_limit = session.get("https://api.github.com/rate_limit", timeout=10).json()
    if rate_limit["rate"]["remaining"] == 0:
      next_window = rate_limit["rate"]["reset"]
      while time.time() < next_window:
        progress.set_description(f"wait {int(next_window - time.time())}s")
        time.sleep(10)
  except Exception:
    time.sleep(60)


def _is_crash(label_name: str) -> bool:
  """A crash label that is not 'crash-on-invalid'."""
  return "crash" in label_name and label_name != "crash-on-invalid"


def fetch(issue_id):
  data_json_path = os.path.join(llvm_helper.dataset_dir, f"{issue_id}.json")
  if os.path.exists(data_json_path):
    return False

  issue_url = f"https://api.github.com/repos/llvm/llvm-project/issues/{issue_id}"
  issue = session.get(issue_url).json()
  if "message" in issue and (
    issue["message"] == "Not Found" or issue["message"] == "This issue was deleted"
  ):
    return False
  if issue["state"] != "closed" or issue["state_reason"] != "completed":
    return False
  if "issue" not in issue["html_url"]:
    return False

  has_valid_label = False
  is_crash = False
  for label in issue["labels"]:
    label_name = label["name"]

    if label_name in _ALWAYS_SKIP_LABELS:
      return False
    if label_name.startswith(_NON_LLVM_PREFIXES):
      return False
    if label_name in {
      "llvm-reduce",
      "llvm:bitcode",
      "llvm:openmpirbuilder",
      "BOLT",
      "mc",
      "libc++",
      "coroutines",
    }:
      return False

    if "hang" in label_name:
      has_valid_label = True
    if _is_crash(label_name):
      has_valid_label = True
      is_crash = True
    if label_name == "miscompilation":
      has_valid_label = True

  for label in issue["labels"]:
    label_name = label["name"]
    if label_name in _BACKEND_LABELS or label_name.startswith("backend:"):
      if is_crash and has_valid_label:
        has_valid_label = True  # backend-crash: keep
      else:
        return False  # backend non-crash: skip

  if not has_valid_label:
    return False

  try:
    out = subprocess.check_output(
      ["python3", postfix_extract, str(issue_id)], stderr=subprocess.DEVNULL
    ).decode()
    if "This issue is marked as invalid" in out:
      return False
    return True
  except subprocess.CalledProcessError:
    return True


os.makedirs(cache_dir, exist_ok=True)
success = 0
progress = tqdm.tqdm(range(issue_id_begin, issue_id_end + 1))
for issue_id in progress:
  progress.set_description(f"Success {success}")
  cache_file = os.path.join(cache_dir, str(issue_id))
  if os.path.exists(cache_file):
    progress.refresh()
    continue
  while True:
    try:
      if fetch(issue_id):
        success += 1
      else:
        Path(cache_file).touch()
      break
    except KeyError:
      wait(progress)
    except requests.exceptions.RequestException:
      wait(progress)
    except ValueError:
      wait(progress)
    except Exception as e:
      print(type(e), e)
      exit(0)
