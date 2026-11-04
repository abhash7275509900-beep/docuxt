import os
import sys

if (
    os.environ.get("DOCENGINE_ENV", "").lower() != "production"
    and os.environ.get("DOCENGINE_DISABLE_COMPAT", "") not in ("1", "true", "yes")
    and os.environ.get("DOCENGINE_NO_COMPAT", "") not in ("1", "true", "yes")
):
    _compat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "_compat")
    if os.path.isdir(_compat) and _compat not in sys.path:
        sys.path.append(_compat)
