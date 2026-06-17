#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["soliplex-concierge>=0.9"]
# ///
"""Apply the 'soliplex-concierge' extension to a generated Soliplex stack.

Thin entry point: 'uv run' provisions 'soliplex-concierge' from the metadata
above, then delegates to 'soliplex_concierge.installer', where all the logic
lives. The room template ships beside this script under 'assets/'; this shim
resolves that dir (the library can't -- it lives in site-packages) and passes
it in. Run with '--help' for the flags.
"""

import pathlib
import sys

from soliplex_concierge import installer

# Bundled assets ship beside this script under '<skill>/assets/' (this file is
# '<skill>/scripts/install_concierge.py'); resolve them relative to __file__.
_ASSETS = pathlib.Path(__file__).resolve().parent.parent / "assets"

if __name__ == "__main__":  # pragma: no cover
    sys.exit(installer.main([str(_ASSETS), *sys.argv[1:]]))
