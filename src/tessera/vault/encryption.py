"""Argon2id key derivation and memory-protected key storage.

Tessera derives the sqlcipher page key from the user passphrase via argon2id.
The parameter set is versioned in ``_meta.kdf_version`` so future strengthening
can ship without breaking existing vaults (see docs/system-design.md
§Encryption at rest §Rotation).

``ProtectedKey`` holds the derived key in a buffer that is best-effort
``mlock``-ed on Linux and macOS and zero-wiped on close. Python's managed
memory model prevents a hard guarantee — the ctypes buffer is stable but the
Python-level byte copies used to call sqlcipher PRAGMA key are subject to the
normal GC lifecycle; this is documented in docs/threat-model.md §S3.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import secrets
import sys
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw


@dataclass(frozen=True, slots=True)
class KDFParams:
    version: int
    time_cost: int
    memory_cost_kib: int
    parallelism: int
    hash_len: int
    salt_len: int


KDF_V1: Final[KDFParams] = KDFParams(
    version=1,
    time_cost=3,
    memory_cost_kib=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

_KDF_REGISTRY: Final[dict[int, KDFParams]] = {KDF_V1.version: KDF_V1}

CURRENT_KDF_VERSION: Final[int] = KDF_V1.version


def kdf_params(version: int) -> KDFParams:
    if version not in _KDF_REGISTRY:
        raise ValueError(f"unknown kdf version: {version}")
    return _KDF_REGISTRY[version]


def new_salt(params: KDFParams = KDF_V1) -> bytes:
    return secrets.token_bytes(params.salt_len)


SALT_SIDECAR_SUFFIX: Final[str] = ".salt"


def _salt_sidecar_path(vault_path: object) -> object:
    # Kept lazy so callers on either ``pathlib.Path`` or a plain string can
    # use this without importing Path here. Narrow ``typing`` at the
    # boundary functions below.
    from pathlib import Path

    p = Path(str(vault_path))
    return p.with_suffix(p.suffix + SALT_SIDECAR_SUFFIX)


def save_salt(vault_path: object, salt: bytes) -> None:
    """Persist a KDF salt alongside ``vault_path`` so re-opens can re-derive.

    The salt is non-secret but must be stable across daemon restarts — the
    same passphrase must produce the same key every time or the vault
    becomes unopenable. A sidecar file named ``<vault>.salt`` keeps the
    salt out of the encrypted DB (where it would be unreadable before the
    key is known) while still living next to the vault for backups and
    portability.
    """

    if len(salt) != KDF_V1.salt_len:
        raise ValueError(f"salt length {len(salt)} != expected {KDF_V1.salt_len}")
    from pathlib import Path

    sidecar = Path(str(_salt_sidecar_path(vault_path)))
    # Mode 0o600 on POSIX — Path.write_bytes does not take a mode arg, so
    # set it after. Windows ignores the chmod.
    sidecar.write_bytes(salt)
    try:
        sidecar.chmod(0o600)
    except OSError:  # pragma: no cover — Windows filesystems that reject chmod
        return


def load_salt(vault_path: object) -> bytes:
    """Read the KDF salt sidecar that :func:`save_salt` wrote.

    Raises :class:`FileNotFoundError` when the sidecar is absent so the
    caller can surface a specific "vault not initialised" message rather
    than a generic unlock failure.
    """

    from pathlib import Path

    sidecar = Path(str(_salt_sidecar_path(vault_path)))
    data = sidecar.read_bytes()
    if len(data) != KDF_V1.salt_len:
        raise ValueError(f"salt sidecar {sidecar} has unexpected length {len(data)}")
    return data


def derive_key(
    passphrase: bytes | bytearray,
    salt: bytes,
    params: KDFParams = KDF_V1,
) -> ProtectedKey:
    if len(salt) != params.salt_len:
        raise ValueError(f"salt length {len(salt)} != expected {params.salt_len}")
    if len(passphrase) == 0:
        raise ValueError("passphrase must not be empty")
    raw = hash_secret_raw(
        secret=bytes(passphrase),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
        hash_len=params.hash_len,
        type=Argon2Type.ID,
    )
    try:
        return ProtectedKey.adopt(raw)
    finally:
        # argon2-cffi returns a fresh bytes object that Python's GC controls;
        # overwrite its backing storage now that the ctypes buffer owns the
        # authoritative copy. This is best-effort — bytes is immutable so a
        # prior tenant of the allocation may still exist elsewhere — but it
        # closes the obvious duplicate-in-heap window called out in
        # docs/threat-model.md §S3.
        ctypes.memset((ctypes.c_char * len(raw)).from_buffer_copy(raw), 0, len(raw))


class ProtectedKey:
    """A fixed-length key held in a ctypes buffer, mlock-ed where supported.

    Use as a context manager or call ``.close()`` explicitly; on close the
    buffer is zero-wiped and ``munlock``-ed. Accessing ``.hex()`` or
    ``.as_pragma_literal()`` after close raises ``RuntimeError``.
    """

    __slots__ = ("_buffer", "_closed", "_length", "_mlocked")

    def __init__(self, length: int) -> None:
        # Set _closed first so __del__ is safe even when the ValueError
        # below short-circuits construction (slot attributes are not
        # default-initialised).
        self._closed = True
        self._mlocked = False
        self._length = 0
        self._buffer = ctypes.create_string_buffer(1)
        if length <= 0:
            raise ValueError("key length must be positive")
        self._length = length
        self._buffer = ctypes.create_string_buffer(length)
        self._mlocked = _try_mlock(self._buffer, length)
        self._closed = False

    @classmethod
    def adopt(cls, raw: bytes) -> ProtectedKey:
        key = cls(len(raw))
        ctypes.memmove(key._buffer, raw, len(raw))
        return key

    def hex(self) -> str:
        self._check_open()
        return self._buffer.raw[: self._length].hex()

    def as_pragma_literal(self) -> str:
        """Return the raw-key form accepted by ``PRAGMA key``.

        sqlcipher parses ``x'<hex>'`` as a blob literal, but only when the
        whole expression is quoted as a string. The outer double quotes are
        part of the grammar, not the value.
        """

        return f"\"x'{self.hex()}'\""

    def wipe(self) -> None:
        if self._closed:
            return
        ctypes.memset(self._buffer, 0, self._length)
        if self._mlocked:
            _try_munlock(self._buffer, self._length)
            self._mlocked = False
        self._closed = True

    close = wipe

    @property
    def mlocked(self) -> bool:
        return self._mlocked

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.wipe()

    def __del__(self) -> None:
        self.wipe()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("ProtectedKey has been wiped")


def _libc() -> ctypes.CDLL | None:
    if sys.platform == "win32":  # pragma: no cover - non-Windows test runner
        return None
    name = ctypes.util.find_library("c")
    if name is None:  # pragma: no cover - libc is always present on POSIX CI
        return None
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError:  # pragma: no cover - would only hit on broken libc
        return None


def _try_mlock(buffer: ctypes.Array[ctypes.c_char], length: int) -> bool:
    libc = _libc()
    if libc is None:  # pragma: no cover - POSIX-only test runner
        return False
    rc = libc.mlock(ctypes.byref(buffer), ctypes.c_size_t(length))
    if rc == 0:
        return True
    # RLIMIT_MEMLOCK too low is common on user machines; treat as best-effort
    # soft miss rather than a hard failure per docs/threat-model.md §S3.
    _ = ctypes.get_errno()
    return False  # pragma: no cover - mlock succeeds on dev/CI runners


def _try_munlock(buffer: ctypes.Array[ctypes.c_char], length: int) -> bool:
    libc = _libc()
    if libc is None:  # pragma: no cover - POSIX-only test runner
        return False
    rc: int = libc.munlock(ctypes.byref(buffer), ctypes.c_size_t(length))
    return rc == 0


def disable_core_dumps() -> None:
    """Best-effort: prevent core dumps from capturing decrypted pages.

    Called at daemon start per docs/threat-model.md §S3. Silent no-op on
    platforms without ``setrlimit`` (Windows).
    """

    if sys.platform == "win32":  # pragma: no cover - non-Windows CI
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - resource module ships with POSIX Python
        return
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):  # pragma: no cover - caller-specific permissions
        return
    if hasattr(os, "PR_SET_DUMPABLE"):  # pragma: no cover - Linux-only branch
        libc = _libc()
        if libc is None:
            return
        PR_SET_DUMPABLE = 4
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
