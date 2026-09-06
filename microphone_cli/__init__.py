"""microphone-cli — agent-first CLI for USB microphones and microphone arrays.

Enumerate what is attached, inspect capture formats, read and set gain, reach the
XVF3800 array firmware (direction-of-arrival, echo-canceller state, raw
parameters), and serve or record audio. Installed as the ``microphone`` command;
the import package is ``microphone_cli`` and the distribution is ``microphone-cli``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("microphone-cli")
except PackageNotFoundError:  # pragma: no cover - editable install without metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
