"""hermes_runtime — run our Python inside Hermes' own runtime, on any Hermes layout.

Before Hermes v0.23 (package manager, "PM") a source install kept one interpreter at
``<home>/hermes-agent/venv/bin/python`` and every script of ours pointed at it. Since the
2026-11-12 update the dependencies live in a PM environment that changes path on every
update (``<home>/installs/<key>/environments/<id>/venv``), the interpreter is a managed
Python 3.14 under ``<home>/tools``, and the old in-tree venv (Python 3.11) is left behind:
it can no longer import Hermes (``No module named 'hermes_yaml'``).

The stable contract Hermes publishes is its launcher, ``<home>/hermes-agent/.hermes/bin/hermes``:
``--print-runtime-command`` prints (JSON) the exact interpreter and the bootstrap it runs
— put the checkout on ``sys.path``, ``import hermes_bootstrap`` (which selects the current
dependency environment) — so we ask it at run time and never store a versioned path.

    from hermes_runtime import module_argv
    os.execv(*module_argv(home, "proactive_intelligence", ["--state", ...], extra_paths=[scripts]))

Python 3.9 compatible (the repo venv runs the wrappers).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

LEGACY_PYTHON = "hermes-agent/venv/bin/python"
LAUNCHER = "hermes-agent/.hermes/bin/hermes"


def launcher(home: Path) -> Optional[Path]:
    p = Path(home) / LAUNCHER
    return p if p.is_file() else None


def base_python(home: Path, timeout: float = 60) -> Tuple[str, bool]:
    """(interpreter, managed). ``managed`` is True for a PM install (the interpreter needs
    ``hermes_bootstrap`` to find its packages), False for a legacy in-tree venv."""
    home = Path(home)
    exe = launcher(home)
    if exe is not None:
        out = subprocess.run([str(exe), "--print-runtime-command", "--module", "hermes_cli.main"],
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode == 0:
            try:
                cmd = json.loads(out.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                cmd = None
            if isinstance(cmd, list) and cmd and isinstance(cmd[0], str) and Path(cmd[0]).is_file():
                return cmd[0], True
    legacy = home / LEGACY_PYTHON
    if legacy.is_file():
        return str(legacy), False
    raise RuntimeError("no Hermes runtime under %s (no launcher, no legacy venv)" % home)


def bootstrap_code(home: Path, module: str, extra_paths: Sequence[str] = ()) -> str:
    """The launcher's own bootstrap, then our paths, then the module as __main__."""
    root = str(Path(home) / "hermes-agent")
    return ("import os, sys, runpy; "
            "[os.environ.pop(k, None) for k in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')]; "
            "sys.path.insert(0, %r); sys._hermes_pin_default_home = True; import hermes_bootstrap; "
            "os.environ.setdefault('HERMES_HOME', %r); "
            "[sys.path.insert(0, p) for p in reversed(%r)]; "
            "runpy.run_module(%r, run_name='__main__', alter_sys=True)"
            % (root, str(home), list(extra_paths), module))


def module_argv(home: Path, module: str, args: Sequence[str] = (),
                extra_paths: Sequence[str] = ()) -> Tuple[str, List[str]]:
    """(executable, argv) to os.execv: run ``module`` with Hermes importable."""
    python, managed = base_python(home)
    if managed:
        return python, [python, "-I", "-c", bootstrap_code(home, module, extra_paths), *args]
    # Legacy venv: its site-packages already hold Hermes' dependencies.
    paths = "".join("sys.path.insert(0, %r); " % p for p in reversed(list(extra_paths)))
    code = "import sys, runpy; %srunpy.run_module(%r, run_name='__main__', alter_sys=True)" % (paths, module)
    return python, [python, "-c", code, *args]


if __name__ == "__main__":  # `python3 hermes_runtime.py [home]` prints what it resolves
    import sys
    h = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes")
    print(json.dumps({"python": base_python(h)[0], "managed": base_python(h)[1]}))
