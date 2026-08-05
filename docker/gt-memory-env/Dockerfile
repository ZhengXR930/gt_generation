FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# libclang-rt-*-dev carries the compiler-rt static runtimes. Without it
# clang accepts -fsanitize=address/fuzzer but the link fails on a missing
# libclang_rt.asan-x86_64.a, so no OSS-Fuzz target can be built and every
# repo-track Stage 01 reproduction fails.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    curl \
    wget \
    xz-utils \
    unzip \
    zip \
    tar \
    file \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    gcc \
    g++ \
    clang \
    lld \
    llvm \
    libclang-rt-18-dev \
    make \
    cmake \
    ninja-build \
    pkg-config \
    autoconf \
    automake \
    bison \
    flex \
    libtool \
    m4 \
    gdb \
    lldb \
    valgrind \
    binutils \
    jq \
    ripgrep \
    libssl-dev \
    libevent-dev \
    libncurses-dev \
    libtinfo6 \
    zlib1g-dev \
    libxml2-dev \
    libpng-dev \
    libjpeg-dev \
    libtiff-dev \
    libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
