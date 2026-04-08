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
        [\x30-\x3f]*  # parameter bytes (0-9 ; < = > ?)
        [\x20-\x2f]*  # intermediate bytes (space ! " # etc.)
        [\x40-\x7e]   # final byte (@ A-Z [ \ ] ^ _ ` a-z { | } ~)
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
