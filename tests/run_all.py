"""Runs every test file. No network, no pytest dependency."""

import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
failed = 0
for path in sorted(here.glob("test_*.py")):
    print(f"\n=== {path.name} ===")
    if subprocess.run([sys.executable, str(path)]).returncode:
        failed += 1
sys.exit(1 if failed else 0)
