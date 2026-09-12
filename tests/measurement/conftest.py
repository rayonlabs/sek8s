import os
import sys

# The measurement engine and the launcher's arg builders now live in one package,
# chutes_cvm (src/chutes-cvm). Put it on sys.path so the tests import
# chutes_cvm.measurement.* and chutes_cvm.guest.* without an install.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
for _p in (os.path.join(_ROOT, "src", "chutes-cvm"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
