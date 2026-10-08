"""The old name of the ``zmart_analysis`` package, kept so older callers work.

ZMART Analysis used to install as a package called ``engine``. That name is
easy to confuse with any other engine on a computer, so the package is now
``zmart_analysis``::

    from zmart_analysis import Engine

This module keeps ``from engine import Engine`` working for the tools that
still use it (the ZMART Interface among them). Nothing new should import
from here.
"""

from zmart_analysis import *  # noqa: F401,F403
from zmart_analysis import __all__, __version__  # noqa: F401
