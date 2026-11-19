# CMake toolchain file: cross-compile for Windows (x86_64) with llvm-mingw.
# Used by .gitlab-ci.yml on a Linux runner; also works locally:
#   cmake -B build-win -DCMAKE_TOOLCHAIN_FILE=ci/mingw-toolchain.cmake
# (llvm-mingw bin/ must be on PATH)

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-clang)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-clang++)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
