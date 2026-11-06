"""Install a private hook receiver independent of development environments and checkouts."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path

from .launcher import _private_directory, _regular_descriptor, default_data_dir

_MODULES = (
    "input_claude",
    "input_codex",
    "input_events",
    "input_extensions",
    "input_receive",
    "launcher",
    "model",
)
_ENTRY = """import sys
try:
    from spiritboard.input_receive import main
    main(sys.argv[1:])
except Exception:
    pass
"""


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in (
            ("__main__.py", _ENTRY.encode()),
            ("spiritboard/__init__.py", b""),
            *(
                (
                    f"spiritboard/{module}.py",
                    files("spiritboard").joinpath(f"{module}.py").read_bytes(),
                )
                for module in _MODULES
            ),
        ):
            archive.writestr(zipfile.ZipInfo(name), content)
    return output.getvalue()


def prepare() -> list[str]:
    """Snapshot only receiver code; hook execution never installs packages or uses the network."""
    frozen = getattr(sys, "frozen", False)
    executable = Path(sys.executable if frozen else sys._base_executable)
    if not executable.is_file():
        raise ValueError("The hook receiver needs an installed Python interpreter")
    payload = executable.read_bytes() if frozen else _archive()
    root = default_data_dir()
    _private_directory(root)
    runtime = root / "input-runtime"
    _private_directory(runtime)
    destination = runtime / hashlib.sha256(payload).hexdigest()
    _private_directory(destination)
    name = (
        ("spiritboard-input.exe" if os.name == "nt" else "spiritboard-input")
        if frozen
        else ("spiritboard-input.pyz")
    )
    path = destination / name
    if path.exists() or path.is_symlink():
        descriptor = _regular_descriptor(path)
        with os.fdopen(descriptor, "rb") as stream:
            if stream.read(len(payload) + 1) != payload:
                raise ValueError("The installed hook receiver has unexpected contents")
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=".receiver-", dir=destination)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                if frozen and os.name == "posix":
                    os.fchmod(stream.fileno(), 0o700)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return (
        [str(path), "input-event"]
        if frozen
        else [str(executable), "-I", "-S", str(path), "input-event"]
    )
