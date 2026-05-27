FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

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
    make \
    cmake \
    ninja-build \
    pkg-config \
    autoconf \
    automake \
    libtool \
    m4 \
    gdb \
    lldb \
    valgrind \
    binutils \
    jq \
    ripgrep \
    libssl-dev \
    zlib1g-dev \
    libxml2-dev \
    libpng-dev \
    libjpeg-dev \
    libtiff-dev \
    libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
