# alive-tv — Translation Verification for LLVM IR

`alive-tv` checks whether a transformation from source IR to target IR
is semantically correct (i.e., the target is a refinement of the source).
It uses an SMT solver to find counterexamples.

## Basic Usage

```bash
# Verify a transformation
alive-tv source.ll target.ll

# Typical workflow: optimize, then verify
opt -passes=instcombine -S input.ll -o output.ll
alive-tv input.ll output.ll

# With increased timeout
alive-tv --smt-to=60000 source.ll target.ll

# Suppress spurious undef failures
alive-tv --disable-undef-input source.ll target.ll
```

## Interpreting Output

### Correct transformation

```
define i32 @test(i32 %x) {
  ...
}
=>
define i32 @test(i32 %x) {
  ...
}

Transformation seems to be correct!

Summary:
  1 correct transformations
  0 incorrect transformations
  0 failed-to-prove transformations
  0 Alive2 errors
```

### Incorrect transformation (miscompilation found)

```
ERROR: Value mismatch

Example:
i32 %x = #x00000001 (1)
i32 %y = #xffffffff (4294967295, -1)

Source value: #x00000000 (0)
Target value: #x00000002 (2)

Summary:
  0 correct transformations
  1 incorrect transformations
  0 failed-to-prove transformations
  0 Alive2 errors
```

The counterexample shows concrete input values that produce different
results in source vs. target. This proves the transformation is wrong.

### Inconclusive (timeout or unsupported)

```
Summary:
  0 correct transformations
  0 incorrect transformations
  1 failed-to-prove transformations
  0 Alive2 errors
```

This means alive-tv **could not prove** correctness. It does NOT mean
the transformation is incorrect. Possible causes:
- SMT solver timeout (increase with `--smt-to`)
- Unsupported IR feature
- Complex transformation beyond solver capacity

### Alive2 error

```
Summary:
  0 correct transformations
  0 incorrect transformations
  0 failed-to-prove transformations
  1 Alive2 errors
```

An internal error in alive-tv itself (e.g., unsupported instruction,
type mismatch). The verification did not complete.

## Flags Reference

| Flag | Description |
|------|-------------|
| `--disable-undef-input` | Skip checks with undef inputs (reduces spurious failures) |
| `--disable-poison-input` | Skip checks with poison inputs |
| `--smt-to=<ms>` | SMT solver timeout in milliseconds (default: 10000) |
| `--src-unroll=<N>` | Unroll loops in source N times (default: 0 = first iteration only, no back-edge; use ≥2 for meaningful loop coverage) |
| `--tgt-unroll=<N>` | Unroll loops in target N times (default: 0 = first iteration only, no back-edge; use ≥2 for meaningful loop coverage) |
| `--bidirectional` | Check both src→tgt and tgt→src |
| `--succinct` | Less verbose output |
| `--tactic-timeout=<ms>` | Per-tactic solver timeout |

## Known Limitations

### Unsupported IR Features

alive-tv does not support all LLVM IR. Known unsupported or partially
supported features include:

- Some vector operations
- Some floating-point edge cases
- External function calls (modeled conservatively)
- Inline assembly
- Some memory model details

### Attribute Sensitivity

The attributes `noalias` and `nofree` can cause spurious "incorrect"
results. The harness's `llvm_verify_ir` tool automatically strips these
before verification.

### Function-Level Only

alive-tv verifies **individual function** transformations. It matches
functions by name between source and target files. It cannot verify:
- Interprocedural transformations
- Module-level changes (globals, metadata)
- Changes that add or remove functions

### Timeout Tuning

The default 10-second timeout is often too short for complex
transformations. Recommended settings:

```bash
# For most cases
alive-tv --smt-to=30000 src.ll tgt.ll

# For complex loop transformations
alive-tv --smt-to=120000 src.ll tgt.ll
```

## Common Use Cases

### Confirm a Miscompilation Bug

```bash
# 1. Get the buggy transformation
opt -passes=instcombine -S repro.ll -o buggy.ll

# 2. Verify — should report "incorrect"
alive-tv repro.ll buggy.ll
```

### Verify Your Fix Is Correct

```bash
# After fixing the LLVM source and rebuilding:
opt -passes=instcombine -S repro.ll -o fixed.ll
alive-tv repro.ll fixed.ll
# Should report "correct"
```

### Check if a Manual Rewrite Is Valid

```bash
# Write your expected output manually
alive-tv before.ll expected.ll
```

## Tips

- **"Incorrect" but you think it's valid?** Check if the source IR has
  undefined behavior. alive-tv may be exposing UB that makes the
  transformation technically incorrect.

- **For crash bugs:** alive-tv is not useful. The issue is a crash, not
  a semantic error. Use `opt` directly to reproduce.

- **Multiple functions:** alive-tv verifies all functions that exist in
  both files. If you only care about one, extract it with
  `llvm-extract -func=<name>`.

## See Also

- alive-tv is part of the [Alive2 project](https://github.com/AliveToolkit/alive2).
- `llvm/docs/LangRef.rst` for IR semantics that alive-tv checks against.
