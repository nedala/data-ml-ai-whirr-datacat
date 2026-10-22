"""Studio test runner (no pytest in the image).

    docker run --rm --entrypoint python -v $PWD/build/studio:/opt/studio \
      -v $PWD/build/ui-kit:/opt/ui-kit -v $PWD:/repo:ro -w /opt/studio \
      bsp/appliance:latest -m tests.run [module ...]

Each tests/test_*.py module runs in its own process against a fresh temp
project directory; its test_* functions run in file order and may share
state (e.g. setup → login → authz)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def main(argv: list[str]) -> int:
    # default: modules written for this runner (module-level test_ functions);
    # unittest-based modules in this folder bring their own runner
    modules = argv or sorted(p.stem for p in HERE.glob("test_*.py")
                             if re.search(r"^def test_", p.read_text(encoding="utf-8"), re.M))
    failed = []
    for mod in modules:
        print(f"\n━━ {mod}", flush=True)
        if subprocess.run([sys.executable, "-m", "tests._runone", mod], cwd=HERE.parent).returncode:
            failed.append(mod)
    print(f"\n{len(modules) - len(failed)}/{len(modules)} modules passed" + (f" — FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
