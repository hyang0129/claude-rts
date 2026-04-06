"""Cross-platform PTY abstraction.

On Windows: delegates to winpty.PtyProcess (ConPTY).
On macOS/Linux: delegates to ptyprocess.PtyProcess (POSIX PTY).

Normalised interface:
  PtyProcess.spawn(cmd, dimensions=(rows, cols))  — cmd is str or list
  pty.read(size=4096)                             — always returns bytes
  pty.write(data)                                 — accepts str or bytes
  pty.setwinsize(rows, cols)
  pty.isalive()                                   — bool
  pty.terminate(force=False)
"""

import shlex
import sys

if sys.platform == "win32":
    from winpty import PtyProcess as _WinPty

    class PtyProcess:
        def __init__(self, proc):
            self._proc = proc

        @classmethod
        def spawn(cls, cmd, dimensions=(24, 80)):
            if isinstance(cmd, list):
                cmd = " ".join(shlex.quote(a) for a in cmd)
            return cls(_WinPty.spawn(cmd, dimensions=dimensions))

        def read(self, size=4096):
            data = self._proc.read()
            if isinstance(data, str):
                return data.encode("utf-8", errors="replace")
            return data

        def write(self, data):
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            self._proc.write(data)

        def setwinsize(self, rows, cols):
            self._proc.setwinsize(rows, cols)

        def isalive(self):
            return self._proc.isalive()

        def terminate(self, force=False):
            self._proc.terminate(force=force)

else:
    from ptyprocess import PtyProcess as _PtyProcess

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
