"""POSIX PTY abstraction (devcontainer/Linux/CI contract).

Delegates to ptyprocess.PtyProcess (POSIX PTY only). The Windows/winpty
branch has been removed; Windows users who need it should install the
optional extra: pip install -e ".[windows]".

If ptyprocess is not installed, a clear ImportError is raised at import time
rather than surfacing a confusing AttributeError later.

Normalised interface:
  PtyProcess.spawn(cmd, dimensions=(rows, cols))  — cmd is str or list
  pty.read(size=4096)                             — always returns bytes
  pty.write(data)                                 — accepts str or bytes
  pty.setwinsize(rows, cols)
  pty.isalive()                                   — bool
  pty.terminate(force=False)
"""

import shlex

try:
    from ptyprocess import PtyProcess as _PtyProcess
except ImportError as exc:
    raise ImportError(
        "ptyprocess is required for PTY support on POSIX systems. "
        "Install it with: pip install ptyprocess\n"
        "On Windows, use: pip install -e '.[windows]' for the winpty backend."
    ) from exc


class PtyProcess:
    def __init__(self, proc):
        self._proc = proc

    @classmethod
    def spawn(cls, cmd, dimensions=(24, 80)):
        argv = shlex.split(cmd) if isinstance(cmd, str) else cmd
        return cls(_PtyProcess.spawn(argv, dimensions=dimensions))

    def read(self, size=4096):
        return self._proc.read(size)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        self._proc.write(data)

    def setwinsize(self, rows, cols):
        self._proc.setwinsize(rows, cols)

    def isalive(self):
        return self._proc.isalive()

    def terminate(self, force=False):
        self._proc.terminate(force=force)
