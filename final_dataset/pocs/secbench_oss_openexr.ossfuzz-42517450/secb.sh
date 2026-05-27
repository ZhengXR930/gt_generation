#!/bin/bash

build() {
    echo "BUILDING THE PROJECT..."

    # Handle git sub-modules
    if [[ -f .gitmodules || -f .gitmodule ]]; then
        echo "Detected git sub-modules - initialising/updating..."
        git submodule update --init --recursive
    else
        echo "No git sub-modules found - skipping update."
    fi

    # Check for repo_changes.diff and apply if it exists and hasn't been applied yet
    if [[ -f /testcase/repo_changes.diff ]]; then
        # Check if the patch has already been applied to avoid re-applying
        if ! git apply --check /testcase/repo_changes.diff &>/dev/null; then
            echo "Repository changes already applied or cannot be applied cleanly. Proceeding with build."
        else
            echo "Applying repository changes from repo_changes.diff..."
            git apply /testcase/repo_changes.diff || echo "Warning: Could not apply repo_changes.diff cleanly. Proceeding anyway."
        fi
    fi

    # stdout: /dev/null
    # stderr: grep filters out "warning:" and lets everything else through
    if /usr/local/bin/compile \
         1>/dev/null \
         2> >(grep -Fv --line-buffered -e "warning:" -e "SyntaxWarning:" -e "WARNING:" >&2); then
        echo "BUILD COMPLETED SUCCESSFULLY!"
    else
        echo "BUILD FAILED!"
        exit 1
    fi
}

repro() {
    /work/bin/exrcheck -c -s /testcase/poc
}

patch() {
    echo "PATCHING THE PROJECT..."
    cd /src/openexr

    if [[ ! -f /testcase/model_patch.diff ]]; then
        echo "ERROR: /testcase/model_patch.diff not found. Please save the file before patching."
        exit 1
    fi

    if git apply /testcase/model_patch.diff; then
        echo "PATCH APPLIED SUCCESSFULLY!"
    else
        echo "PATCH APPLICATION FAILED!"
        exit 1
    fi
}


if [ "$#" -ge 1 ]; then
    command="$1"

    case "$command" in
        build)
            build "$@"
            ;;
        repro)
            repro "$@"
            ;;
        patch)
            patch "$@"
            ;;
        *)
            echo "Unknown command: $command"
            echo "Usage: secb [build|repro|patch]"
            exit 1
            ;;
    esac
else
    echo "Usage: secb [build|repro|patch]"
    exit 1
fi
