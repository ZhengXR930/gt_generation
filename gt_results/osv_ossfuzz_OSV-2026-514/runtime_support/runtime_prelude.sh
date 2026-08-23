#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# Upstream builder checks out tmux and tmux-fuzzing-corpus as siblings.
ln -sfn . tmux

if [ ! -d tmux-fuzzing-corpus/.git ]; then
  git clone --depth 1 https://github.com/tmux/tmux-fuzzing-corpus.git tmux-fuzzing-corpus
fi
