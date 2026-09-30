"""Minimal test runner so the tests work without installing pytest.

    python tests/run_tests.py
"""
import importlib.util
import sys
import time
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
failures = 0
for path in sorted(here.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        t0 = time.time()
        try:
            getattr(mod, name)()
            print(f"PASS  {name}  ({time.time() - t0:.1f}s)")
        except Exception:                      # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
print(f"\n{'All tests passed' if not failures else f'{failures} test(s) failed'}")
sys.exit(1 if failures else 0)
