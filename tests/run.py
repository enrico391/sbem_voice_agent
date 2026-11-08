"""Run the offline suite with uv run python tests/run.py."""

import sys
import unittest
from pathlib import Path

from loguru import logger

# uvloop also avoids restricted-sandbox socketpair wakeups in executor tests.
try:
    import uvloop
except ImportError:
    pass
else:
    uvloop.install()

logger.remove()
logger.add(sys.stderr, level="WARNING")
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
)
raise SystemExit(not result.wasSuccessful())
