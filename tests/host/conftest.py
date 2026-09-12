import os
import sys

# chutes_cvm is a src/ package; add src/chutes-cvm/ to sys.path so that
# `from chutes_cvm.guest.gpu.profiles import ...` works in tests without an install.
_PKG = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir, "src", "chutes-cvm"
)
if os.path.abspath(_PKG) not in sys.path:
    sys.path.insert(0, os.path.abspath(_PKG))
