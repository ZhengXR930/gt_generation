======================
QuickJS Fuzzing Corpus
======================

Overview
========

This repository provides a test corpus for fuzzing the `fuzz_eval` and
`fuzz_compile` targets of QuickJS. The corpus has been derived from JavaScript
sources in the `tests` and `examples` directories of `QuickJS`_.

.. _QuickJS: https://github.com/bellard/quickjs

Corpus Preparation
==================

The following steps were taken to adapt the original tests into a more
effective fuzzing corpus:

1. **Elimination of Secondary Files**: Tests that depend on secondary files
(such as workers or non-standard imports) have been removed to ensure that
each corpus item is independent.

2. **Elimination of Standard Imports**: The import of `std` and `os` modules
have been removed since they are imported by the fuzz targets by default.

3. **Modification of Assertion Functions**: Functions like `assert()` and
`assertThrows()` have been converted to no-op functions. This prevents
continuous errors when the fuzzer randomly mutates parts of the tests.

4. **Decomposition of Complex Tests**: Tests consisting of multiple functions
have been separated into smaller, more focused units. This increases the
granularity of the tests, enhancing the effectiveness of the fuzzing process.

5. **Removal of Benchmarking Loops**: Loops that were originally used for
benchmarking purposes have been removed. Repeating the same code multiple
times is unlikely to trigger new coverage and can slow down the fuzzing
process.

Usage
=====

To use this corpus with libFuzzer, follow these steps:

1. **Build QuickJS with Fuzzing Support**:

    CONFIG_CLANG=y make fuzz_eval

    CONFIG_CLANG=y make fuzz_compile

2. **Run the Fuzzer**:

    ./fuzz_eval <path_to_corpus> ...

    ./fuzz_compile <path_to_corpus> ...
