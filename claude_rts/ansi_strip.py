"""Utility to strip ANSI escape codes from terminal output."""

import re

# Matches all common ANSI escape sequences:
# - CSI sequences: ESC [ ... final_byte
# - OSC sequences: ESC ] ... (ST or BEL terminated)
# - Other ESC sequences: ESC followed by a single character
_ANSI_RE = re.compile(
    r"""
    \x1b       # ESC character
    (?:
        \[     # CSI: ESC [
        [0-9;]*  # parameter bytes
        [A-Za-z]  # final byte
    |
        \]     # OSC: ESC ]
        .*?    # payload
        (?:\x1b\\|\x07)  # ST (ESC \) or BEL
    |
        [^[\]]  # other ESC + single char (e.g. ESC M, ESC 7)
    )
    """,
    re.VERBOSE | re.DOTALL,
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)
