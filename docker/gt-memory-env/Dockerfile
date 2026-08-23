FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# The package set below is not hand-picked: it is the union of the apt
# dependencies declared by the OSS-Fuzz project recipes for every project still
# in the SEC-bench/OSV backlog, so a sample fails on its own build logic rather
# than on a missing header. Re-derive with scripts/survey_ossfuzz_projects.py when the
# backlog changes.
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
    # --- build drivers OSS-Fuzz recipes rely on (samples affected in comments)
    scons \
    meson \
    libtool-bin \
    autogen \
    gperf \
    re2c \
    ragel \
    nasm \
    yasm \
    texinfo \
    gettext \
    bsdmainutils \
    rsync \
    pkgconf \
    subversion \
    # --- language runtimes a handful of targets build through
    python3-setuptools \
    python3-wheel \
    python3-six \
    python-is-python3 \
    ruby \
    ruby-dev \
    cargo \
    rustc \
    default-jdk \
    # --- headers the surveyed projects configure against
    libnuma-dev \
    libpcre2-dev \
    libjson-c-dev \
    libgif-dev \
    libbz2-dev \
    liblzma-dev \
    libcurl4-gnutls-dev \
    libyaml-dev \
    libapr1-dev \
    libaprutil1-dev \
    libmbedtls-dev \
    libopus-dev \
    libvpx-dev \
    libgcrypt20-dev \
    libsqlite3-dev \
    libreadline-dev \
    libgoogle-perftools-dev \
    openssl \
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
    libc++-dev \
    libc++abi-dev \
    libnl-3-dev \
    libnl-genl-3-dev \
    libnl-route-3-dev \
    universal-ctags \
    zlib1g-dev \
    libxml2-dev \
    libpng-dev \
    libjpeg-dev \
    libtiff-dev \
    libzstd-dev \
    libfreetype-dev \
    libharfbuzz-dev \
    libjbig2dec0-dev \
    liblcms2-dev \
    libgumbo-dev \
    libopenjp2-7-dev \
    libonig-dev \
    # --- declared by the remaining OSV backlog's OSS-Fuzz recipes
    # (dataset/ossfuzz_project_config/*.json; re-derive after a backlog change)
    libboost-dev \
    libboost-system-dev \
    libboost-thread-dev \
    libicu-dev \
    icu-devtools \
    libre2-dev \
    libgflags-dev \
    libgoogle-glog-dev \
    protobuf-compiler \
    libglib2.0-dev \
    libaudit-dev \
    libcap-dev \
    libcap-ng-dev \
    libcunit1-dev \
    luajit \
    libbrotli-dev \
    libexpat1-dev \
    libfftw3-dev \
    libgtest-dev \
    libsodium-dev \
    libwebsockets-dev \
    libpcre3-dev \
    python3-dev \
    autopoint \
    gawk \
    ccache \
    # Deliberately not installed, so they are not re-added:
    #   libcurl4-openssl-dev (tarantool) conflicts with libcurl4-gnutls-dev,
    #     which htslib needs -- apt cannot hold both, and htslib is the
    #     heavier user.
    #   python, libz-dev, ncurses-dev, libncurses5-dev, git-core, default-jre
    #     are virtual/transitional names already satisfied above.
    #   python3-distutils and openjdk-11-jdk do not exist on 24.04.
    #   coreutils, gzip, sed are in the base image.
    #   tshark, devscripts, equivs, cmake-curses-gui are test or packaging
    #     tooling, not build inputs.
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
