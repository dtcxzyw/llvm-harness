import glob
import json
import os

from unidiff import PatchSet

import harness.llvm.intern.llvm as llvm_helper

bug_type = {
  "miscompilation": 0,
  "crash": 0,
  "backend-miscompilation": 0,
}
components = {}
labels = {}
count = 0
changed_files_count = []
ins_lines_count = []
del_lines_count = []
modified_lines_count = []
test_count = []
single_file_fix_count = 0
single_function_fix_count = 0
single_hunk_fix_count = 0
verified_count = 0
reproducer_count = 0
reproducer_size_loc = 0
comp_regressions_count = 0

for name in os.listdir(llvm_helper.dataset_dir):
  if name.endswith(".json"):
    file = os.path.join(llvm_helper.dataset_dir, name)
    with open(file) as f:
      data = json.load(f)
    count += 1
    reproducer_count += len(data["tests"])
    reproducer_size_loc += sum(
      [
        sum(len(test["test_body"].splitlines()) for test in test_group["tests"])
        for test_group in data["tests"]
      ]
    )
    comp_regressions_count += sum(
      [
        len(glob.glob("*.ll", root_dir=os.path.join(llvm_helper.llvm_dir, d)))
        for d in data["lit_test_dir"]
      ]
    )
    bug_type[data["bug_type"]] += 1
    hints = data["hints"]
    for component in hints["components"]:
      if component not in components:
        components[component] = 1
      else:
        components[component] += 1
    changed_files = len(hints["bug_location_lineno"])
    changed_files_count.append(changed_files)
    if changed_files == 1:
      single_file_fix_count += 1
    issue = data["issue"]
    for label in issue["labels"]:
      if label not in labels:
        labels[label] = 1
      else:
        labels[label] += 1
    ins_lines = 0
    del_lines = 0
    patchset = PatchSet(data["patch"])
    hunk_count = 0
    for patch in patchset:
      for hunk in patch:
        hunk_count += 1
        ins_lines += hunk.added
        del_lines += hunk.removed
    if hunk_count == 1:
      single_hunk_fix_count += 1
    ins_lines_count.append(ins_lines)
    del_lines_count.append(del_lines)
    modified_lines_count.append(ins_lines + del_lines)
    test_num = 0
    for test in data["tests"]:
      test_num += len(test["tests"])
    test_count.append(test_num)
    bug_func = data["hints"]["bug_location_funcname"]
    bug_func_count = 0
    for k, v in bug_func.items():
      bug_func_count += len(v)
    if bug_func_count == 1 or hunk_count == 1:
      single_function_fix_count += 1
    if data.get("verified", False):
      verified_count += 1


print(f"Total issues: {count}")
print(f"Verified issues: {verified_count} ({verified_count / count * 100.0:.2f}%)")
print("\nBug type summary:")
for k, v in bug_type.items():
  print(f"  {k}: {v}")

print("\nReproducer summary:")
print(f"  Total reproducers: {reproducer_count}")
print(f"  Average reproducers per issue: {reproducer_count / count:.2f}")
print(f"  Average lines of code: {reproducer_size_loc / reproducer_count:.2f}")

print("\nPass regression summary:")
print(f"  Total pass regressions: {comp_regressions_count}")
print(f"  Average pass regressions per issue: {comp_regressions_count / count:.2f}")

print(f"\nBug component summary (Total = {len(components)}):")
for k, v in sorted(components.items(), key=lambda x: x[1], reverse=True):
  print(f"  {k}: {v}")

print("\nLabel summary:")
for k, v in sorted(labels.items(), key=lambda x: x[1], reverse=True):
  print(f"  {k}: {v}")

print("\nChanged files count summary:")
print(f"  Average: {sum(changed_files_count) / count:.2f}")
print(f"  Max: {max(changed_files_count)}")
print(f"  Min: {min(changed_files_count)}")
print(f"  Median: {sorted(changed_files_count)[count // 2]}")

print("\nModified lines summary:")
print(f"  Average: {sum(modified_lines_count) / count:.2f}")
print(f"  Max: {max(modified_lines_count)}")
print(f"  Min: {min(modified_lines_count)}")
print(f"  Median: {sorted(modified_lines_count)[count // 2]}")

print("\nInserted lines summary:")
print(f"  Average: {sum(ins_lines_count) / count:.2f}")
print(f"  Max: {max(ins_lines_count)}")
print(f"  Min: {min(ins_lines_count)}")
print(f"  Median: {sorted(ins_lines_count)[count // 2]}")

print("\nDeleted lines summary:")
print(f"  Average: {sum(del_lines_count) / count:.2f}")
print(f"  Max: {max(del_lines_count)}")
print(f"  Min: {min(del_lines_count)}")
print(f"  Median: {sorted(del_lines_count)[count // 2]}")

print("\nTest count summary:")
print(f"  Average: {sum(test_count) / count:.2f}")
print(f"  Max: {max(test_count)}")
print(f"  Min: {min(test_count)}")
print(f"  Median: {sorted(test_count)[count // 2]}")

print("\nPatch summary:")
print(
  f"  Single file fix: {single_file_fix_count} ({single_file_fix_count / count * 100.0:.2f}%)"
)
print(
  f"  Single func fix: {single_function_fix_count} ({single_function_fix_count / count * 100.0:.2f}%)"
)
print(
  f"  Single hunk fix: {single_hunk_fix_count} ({single_hunk_fix_count / count * 100.0:.2f}%)"
)
