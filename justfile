# ===============================================================================
# MAIN JUSTFILE - Orchestrates all recipe sources
# ===============================================================================

# Show available commands
[group('info')]
help:
    @just --list

# Import devcontainer-managed base recipes (replaced on upgrade).
# Optional with `import?` — not every checkout has the devcontainer
# files (e.g. CI runs outside the container, local non-devcontainer
# setups). Without `?` the whole justfile fails to parse when these
# are missing, which also trips the `just --fmt` pre-commit hook.

import? '.devcontainer/justfile.devc'
import? '.devcontainer/justfile.gh'

# Import team-shared project recipes (git-tracked, preserved on upgrade)

import? 'justfile.project'

# Import personal recipes (gitignored, preserved on upgrade)

import? 'justfile.local'
