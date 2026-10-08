# SPDX-License-Identifier: Apache-2.0
"""Test bootstrap: expose the development-tool PoC package without packaging it."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
