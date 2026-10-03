"""Run every test_* function in tests/ (no pytest needed; pytest works too).

    python tests/run_tests.py
"""
from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    failed = 0
    for path in sorted(HERE.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in sorted(n for n in dir(module) if n.startswith("test_")):
            start = time.time()
            try:
                getattr(module, name)()
                print(f"PASS  {name}  ({time.time() - start:.1f}s)")
            except Exception:                                        # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}\n{traceback.format_exc()}")
    print("All tests passed" if not failed else f"{failed} test(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
