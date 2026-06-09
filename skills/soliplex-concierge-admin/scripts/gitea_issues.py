#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["soliplex-concierge>=0.6"]
# ///
"""Read and resolve Soliplex room-request issues on a Gitea repository.

Thin entry point: `uv run` provisions `soliplex-concierge` (and httpx) from the
inline metadata above, then this delegates to `soliplex_concierge.gitea_admin`,
where all the logic lives (shared with the copy the `-installer` skill drops
into a stack's `scripts/`). Run `gitea_issues.py --help` for the subcommands.
"""

import sys

from soliplex_concierge.gitea_admin import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
