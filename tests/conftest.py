from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must happen before anything imports hookdeck.adapter.
from tests import hermes_stub

hermes_stub.install()
