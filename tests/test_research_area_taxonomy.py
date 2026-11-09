"""Legacy executable wrapper for the taxonomy-independence pytest suite.

The former fail-closed taxonomy tests intentionally no longer apply. This file
remains so the established developer command continues to work.
"""

from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    target = Path(__file__).with_name("test_taxonomy_independence.py")
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(target)]))
