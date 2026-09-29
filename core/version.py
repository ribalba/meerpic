"""The single source of the version number.

Read from the VERSION file at the repository root so that the file the release
tooling edits is the file the running process reports. Nothing here is
duplicated in code, and a container built from a dirty tree cannot claim a
number the tree does not have.
"""

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

try:
    VERSION = VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError:  # running from a wheel or an image that dropped the file
    VERSION = "0.0.0"


def _parts(value: str) -> tuple[int, ...]:
    """A version string as numbers, ignoring anything that is not one.

    Deliberately forgiving: "0.2.1", "v0.2.1" and "0.2.1-rc2" all compare as
    (0, 2, 1). A release scheme that grows a suffix later should not make an
    installed copy think it is ahead of the world.
    """
    out = []
    for chunk in value.strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def is_outdated(current: str, latest: str) -> bool:
    """Is ``latest`` a newer release than ``current``?

    Compared as numbers rather than as text, because "0.10.0" is newer than
    "0.9.0" and sorts before it. Anything unparsable on either side answers
    False: a banner that appears because a proxy served an HTML error page is
    worse than no banner.
    """
    a, b = _parts(current), _parts(latest)
    if not a or not b:
        return False
    # Zero-pad so 0.2 and 0.2.0 are the same release rather than a downgrade.
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return b > a
