import os
import sys

# Optional fallback shim loader for restricted environments without third-party
# wheels installed. Disabled when DOCENGINE_ENV=production, DOCENGINE_DISABLE_COMPAT=1,
# or DOCENGINE_NO_COMPAT=1. Appended to the end of sys.path so real installed
# packages always take precedence.
if (
    os.environ.get("DOCENGINE_ENV", "").lower() != "production"
    and os.environ.get("DOCENGINE_DISABLE_COMPAT", "") not in ("1", "true", "yes")
    and os.environ.get("DOCENGINE_NO_COMPAT", "") not in ("1", "true", "yes")
):
    _compat_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_compat")
    if os.path.isdir(_compat_dir) and _compat_dir not in sys.path:
        sys.path.append(_compat_dir)
