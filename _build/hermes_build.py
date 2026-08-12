"""Build backend that stamps the resolved version into the dashboard manifest.

Hermes discovers dashboard extensions by reading
``hookdeck/dashboard/manifest.json`` off disk, and displays the ``version`` it
finds there (defaulting to ``0.0.0``). That is a second place the version has
to appear — and a literal in a tracked file is exactly what drifted before, so
it is generated here instead, from the same tag setuptools-scm uses.

This is a thin wrapper: every PEP 517 hook is setuptools', except that the two
build hooks stamp the manifest first and put it back afterwards.

The ordering matters and is the whole reason this file is not three lines.
Writing to a tracked file makes the working tree dirty, and setuptools-scm
appends a ``.dYYYYMMDD`` local segment to a dirty tree — so a naive "write the
file, then build" produces an artifact whose manifest and metadata disagree,
which is the bug this is meant to prevent. So: resolve the version while the
tree is still clean, pin it through ``SETUPTOOLS_SCM_PRETEND_VERSION`` so the
later resolution cannot change its mind, and only then touch the file.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from setuptools import build_meta as _setuptools

# Re-export the hooks we do not override, so this is a drop-in backend. PEP 517
# consults the module namespace, not a class, and a missing optional hook
# changes pip's behaviour rather than erroring.
build_editable = _setuptools.build_editable
get_requires_for_build_editable = _setuptools.get_requires_for_build_editable
get_requires_for_build_sdist = _setuptools.get_requires_for_build_sdist
get_requires_for_build_wheel = _setuptools.get_requires_for_build_wheel
prepare_metadata_for_build_editable = _setuptools.prepare_metadata_for_build_editable
prepare_metadata_for_build_wheel = _setuptools.prepare_metadata_for_build_wheel

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _ROOT / "hookdeck" / "dashboard" / "manifest.json"
_PRETEND = "SETUPTOOLS_SCM_PRETEND_VERSION"


def _resolve_version() -> str | None:
    """The version this build will carry, resolved before anything is written.

    Mirrors what setuptools-scm itself will conclude: the git tag when there is
    a repository, and the recorded version when building from an sdist, which
    has no git but does have PKG-INFO. Returns None when neither is available,
    in which case the manifest is left alone rather than stamped with a guess.
    """
    pretended = os.environ.get(_PRETEND)
    if pretended:
        return pretended

    try:
        from setuptools_scm import get_version

        return get_version(root=str(_ROOT))
    except Exception:
        pass

    pkg_info = _ROOT / "PKG-INFO"
    if pkg_info.is_file():
        for line in pkg_info.read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                return line.partition(":")[2].strip()
            if not line.strip():
                break  # end of the header block
    return None


@contextmanager
def _manifest_stamped_with_version():
    version = _resolve_version()
    if version is None or not _MANIFEST.is_file():
        yield
        return

    original = _MANIFEST.read_text(encoding="utf-8")
    data = json.loads(original)
    # Insert after "icon" if present, else append — position is cosmetic, but a
    # stable one keeps the diff of a built artifact readable.
    stamped = {}
    for key, value in data.items():
        stamped[key] = value
        if key == "icon":
            stamped["version"] = version
    stamped.setdefault("version", version)

    previous_pretend = os.environ.get(_PRETEND)
    # Pin the version before dirtying the tree, so the resolution setuptools-scm
    # does inside the build cannot come back different.
    os.environ[_PRETEND] = version
    _MANIFEST.write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")
    try:
        yield
    finally:
        _MANIFEST.write_text(original, encoding="utf-8")
        if previous_pretend is None:
            os.environ.pop(_PRETEND, None)
        else:
            os.environ[_PRETEND] = previous_pretend


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with _manifest_stamped_with_version():
        return _setuptools.build_wheel(
            wheel_directory, config_settings, metadata_directory
        )


def build_sdist(sdist_directory, config_settings=None):
    with _manifest_stamped_with_version():
        return _setuptools.build_sdist(sdist_directory, config_settings)
