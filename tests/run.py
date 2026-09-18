#!/usr/bin/env python3
"""Run the whole suite: python3 tests/run.py [-v] [pattern]

    python3 tests/run.py                 everything
    python3 tests/run.py -v              everything, one line per test
    python3 tests/run.py gesture         only tests/test_gestures.py

No dependencies beyond the standard library, and nothing it does touches the
kernel, the running daemon or the desktop.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    words = [arg for arg in argv[1:] if not arg.startswith("-")]
    pattern = "test_*%s*.py" % words[0] if words else "test_*.py"

    sys.path.insert(0, HERE)
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern=pattern, top_level_dir=HERE)
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
