from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = Path("/Users/benigeri/.hermes/hermes-agent")
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))


def load_plugin_pkg(name: str = "zoom_meeting_chat_testpkg"):
    for mod in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    assert spec.loader is not None
    spec.loader.exec_module(pkg)
    return pkg
