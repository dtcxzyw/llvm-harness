# llvm-patchreview

This skill reviews LLVM patches based on our findings (see [our paper](https://arxiv.org/abs/2603.20075)).

Specifically, this patch focuses on six types of potential mistakes that may arise in an LLVM patch:

1. **Unexpected Assertion Changes**
2. **Unexpected Bypasses**
3. **Introduction of New Correctness Bugs**
4. **Lack of Generality**
5. **New Performance Issues**
6. **Code Smells / Style Issues**

During the review process, the agent has access to [an experience database](./references/) established by [Archer](https://github.com/cuhk-s3/Archer/), which it can utilize to avoid mistakes that were encountered in previous patches, and to identify regressions by cross-referencing known historical issues.
