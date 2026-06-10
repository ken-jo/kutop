"""The legacy ``kubetop`` name must stay a working alias for ``kutop``.

Covers the import-time shim (``kubetop/__init__.py`` re-exposing kutop's
package path + version) and the ``python -m kubetop`` entrypoint.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import kutop


def test_import_kubetop_matches_kutop_version() -> None:
    import kubetop

    assert kubetop.__version__ == kutop.__version__


def test_legacy_kubetop_submodule_imports_resolve_to_kutop() -> None:
    # the shim sets kubetop.__path__ = kutop.__path__, so legacy imports such
    # as ``import kubetop.config`` keep resolving to kutop's modules
    import kubetop.config as alias_config
    import kutop.config as real_config

    assert alias_config.__file__ == real_config.__file__
    assert alias_config.CONFIG_PATH == real_config.CONFIG_PATH
    assert callable(alias_config.load_config)


def test_python_dash_m_kubetop_version_exits_zero() -> None:
    if not sys.executable:
        pytest.skip("sys.executable unavailable; cannot launch a subprocess")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "kubetop", "--version"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"environment cannot run subprocesses: {exc}")

    assert proc.returncode == 0
    assert f"kutop {kutop.__version__}" in proc.stdout
