"""PEP 656 support.

This module implements logic to detect if the currently running Python is
linked against musl, and what musl version is used.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from typing import TYPE_CHECKING, NamedTuple

from ._elffile import ELFFile

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


class _MuslVersion(NamedTuple):
    major: int
    minor: int


def _parse_musl_version(output: str) -> _MuslVersion | None:
    lines = [n for n in (n.strip() for n in output.splitlines()) if n]
    if len(lines) < 2 or lines[0][:4] != "musl":
        return None
    m = re.match(r"Version (\d+)\.(\d+)", lines[1])
    if not m:
        return None
    return _MuslVersion(major=int(m.group(1)), minor=int(m.group(2)))


@functools.lru_cache
def _get_musl_version(executable: str) -> _MuslVersion | None:
    """Detect currently-running musl runtime version.

    This is done by checking the specified executable's dynamic linking
    information, and invoking the loader to parse its output for a version
    string. If the loader is musl, the output would be something like::

        musl libc (x86_64)
        Version 1.2.2
        Dynamic Program Loader
    """
    try:
        with open(executable, "rb") as f:
            ld = ELFFile(f).interpreter
    except (OSError, TypeError, ValueError):
        return None
    if ld is None or "musl" not in ld:
        return None
    proc = subprocess.run([ld], check=False, stderr=subprocess.PIPE, text=True)
    return _parse_musl_version(proc.stderr)


def platform_tags(
    archs: Sequence[str], *, musl_version: _MuslVersion | None = None
) -> Iterator[str]:
    """Generate musllinux tags compatible to the current platform.

    :param archs: Sequence of compatible architectures.
        The first one shall be the closest to the actual architecture and be the part of
        platform tag after the ``linux_`` prefix, e.g. ``x86_64``.
        The ``linux_`` prefix is assumed as a prerequisite for the current platform to
        be musllinux-compatible.
    :param musl_version: The musl version of the target platform. When
        ``None`` (the default), the version is detected from the running
        executable. When provided, the version is trusted as-is, allowing
        tags to be generated for a machine other than the current one.

    :returns: An iterator of compatible musllinux tags.
    """
    if musl_version is None:
        musl_version = _get_musl_version(sys.executable)
        if musl_version is None:  # Python not dynamically linked against musl.
            return
    for arch in archs:
        for minor in range(musl_version.minor, -1, -1):
            yield f"musllinux_{musl_version.major}_{minor}_{arch}"


if __name__ == "__main__":  # pragma: no cover
    import sysconfig

    plat = sysconfig.get_platform()
    assert plat.startswith("linux-"), "not linux"

    print("plat:", plat)
    print("musl:", _get_musl_version(sys.executable))
    print("tags:", end=" ")
    for t in platform_tags([re.sub(r"[.-]", "_", plat.split("-", 1)[-1])]):
        print(t, end="\n      ")
