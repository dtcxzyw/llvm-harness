#! /bin/bash

#-=============================================================================
# This script is used to define the dependencies of llvm-harness.
# - LLVM: https://github.com/llvm/llvm-project
# - alive2: https://github.com/AliveToolkit/alive2
# - Required python dependencies: requirements.txt
#-=============================================================================


if [[ -z ${LLVM_HARNESS_DEPS_DIR} ]]; then
  echo "Error: LLVM_HARNESS_DEPS_DIR is not set. Please set it to the directory where you want to install dependencies."
  return 1
elif [[ ! -d ${LLVM_HARNESS_DEPS_DIR} ]]; then
  mkdir -p ${LLVM_HARNESS_DEPS_DIR}
fi


#-================================
# LLVM
#-================================

DEP_LLVM_DIR=${LLVM_HARNESS_DEPS_DIR}/llvm
DEP_LLVM_VERSION=llvmorg-22.1.0
DEP_LLVM_SOURCE_DIR=${DEP_LLVM_DIR}/llvm
DEP_LLVM_BUILD_DIR=${DEP_LLVM_DIR}/build

DEP_CCACHE_DIR=${LLVM_HARNESS_DEPS_DIR}/ccache
DEP_CCACHE_VERSION=4.11.3
DEP_CCACHE_SOURCE_DIR=${DEP_CCACHE_DIR}/ccache-${DEP_CCACHE_VERSION}
DEP_CCACHE_BUILD_DIR=${DEP_CCACHE_DIR}/build

#-================================
# alive2
#-================================

DEP_ALIVE2_DIR=${LLVM_HARNESS_DEPS_DIR}/alive2
DEP_ALIVE2_VERSION=0dc2be5f04ccb61caebb909a610968cb2348f196
DEP_ALIVE2_SOURCE_DIR=${DEP_ALIVE2_DIR}/alive2
DEP_ALIVE2_BUILD_DIR=${DEP_ALIVE2_DIR}/build

# Z3

DEP_Z3_DIR=${LLVM_HARNESS_DEPS_DIR}/z3
DEP_Z3_VERSION=4.15.2
DEP_Z3_SOURCE_DIR=${DEP_Z3_DIR}/z3-z3-${DEP_Z3_VERSION}
DEP_Z3_BUILD_DIR=${DEP_Z3_DIR}/build

# re2c

DEP_RE2C_DIR=${LLVM_HARNESS_DEPS_DIR}/re2c
DEP_RE2C_VERSION=4.3
DEP_RE2C_SOURCE_DIR=${DEP_RE2C_DIR}/re2c-${DEP_RE2C_VERSION}
DEP_RE2C_BUILD_DIR=${DEP_RE2C_DIR}/build

#-================================
# backend-tv
#-================================

DEP_BACKEND_TV_DIR=${LLVM_HARNESS_DEPS_DIR}/backend-tv
DEP_BACKEND_TV_VERSION=d580e3942720b36eda7e4046be92562fb9452dbd
DEP_BACKEND_TV_SOURCE_DIR=${DEP_BACKEND_TV_DIR}/backend-tv
DEP_BACKEND_TV_BUILD_DIR=${DEP_BACKEND_TV_DIR}/backend-tv/build

# ANTLR4

DEP_ANTLR4_DIR=${LLVM_HARNESS_DEPS_DIR}/altlr4
DEP_ANTLR4_VERSION=4.13.2
DEP_ANTLR4_SOURCE_DIR=${DEP_ANTLR4_DIR}/antlr4
DEP_ANTLR4_BUILD_DIR=${DEP_ANTLR4_DIR}/build

#-================================
# llubi (legacy)
#-================================

DEP_LLUBI_LEGACY_DIR=${LLVM_HARNESS_DEPS_DIR}/llubi-legacy
DEP_LLUBI_LEGACY_VERSION=9798ef7520061b89485475c9739a8c578528f3f7
DEP_LLUBI_LEGACY_SOURCE_DIR=${DEP_LLUBI_LEGACY_DIR}/llubi
DEP_LLUBI_LEGACY_BUILD_DIR=${DEP_LLUBI_LEGACY_DIR}/build

#-================================
# Python dependencies
#-================================

DEP_PY3_VERSION=$(gdb --nx -batch -ex "python import platform; v=platform.python_version().split('.'); print(v[0]+'.'+v[1])")
DEP_PY3_VENV_DIR=${LLVM_HARNESS_DEPS_DIR}/py3_venv
