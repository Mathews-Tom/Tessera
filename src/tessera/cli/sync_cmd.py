"""``tessera sync`` — BYO sync CLI surface (V0.5-P9b).

Per ADR-0022 D4 the CLI exposes four subcommands at v0.5:

* ``tessera sync setup`` — persist the S3 target config + creds.
  Interactive when run from a TTY; flag-driven for scripts.
* ``tessera sync status`` — report the configured store, the
  local watermark, store reachability, and the latest manifest
  sequence on the store.
* ``tessera sync push`` — push the current vault snapshot to the
  configured store via :func:`tessera.sync.push.push`.
* ``tessera sync pull`` — pull the latest snapshot from the
  configured store, updating the watermark on success.

The ``conflicts`` subcommand from ADR-0022 D4 is deferred:
filesystem-store CLI support is not in v0.5-P9b's scope, and S3
stores have no "conflicted copy" semantics. When filesystem-store
CLI support lands, ``conflicts`` is the surface that triages
sync-provider artefacts (Dropbox conflict files, etc.).

Each subcommand follows the existing CLI pattern (see
:mod:`tessera.cli.audit_cmd`): resolve vault path + passphrase via
:mod:`tessera.cli._common`, open the vault, do the work, return an
exit code. Errors surface via the shared ``warn`` / ``fail``
helpers.
"""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Final

import sqlcipher3

from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._ui import info, success, warn
from tessera.sync import config as sync_config
from tessera.sync.pull import PullError, pull
from tessera.sync.push import PushChainBreakError, PushError, push
from tessera.sync.s3 import (
    S3BlobStore,
    S3BlobStoreError,
    S3BucketUnreachableError,
)
from tessera.sync.watermark import (
    CorruptWatermarkError,
    read_watermark,
    store_identity,
    write_watermark,
)
from tessera.vault import keyring_cache
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, load_salt

_HELP_DESCRIPTION: Final[str] = (
    "Configure and operate the BYO sync target (V0.5-P9b).\n\n"
    "The vault is encrypted client-side: the configured S3 endpoint\n"
    "sees only AES-256-GCM ciphertext and a signed monotonic manifest.\n"
    "Per ADR-0022 D5, the S3 endpoint is the only outbound surface\n"
    "added at v0.5; no telemetry or analytics ever leave the host.\n\n"
    "Subcommands: setup | status | push | pull"
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "sync",
        help="BYO sync to an S3-compatible target",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync_subparsers = parser.add_subparsers(dest="sync_command")

    setup_parser = sync_subparsers.add_parser(
        "setup",
        help="configure the S3 target and store credentials",
        description=(
            "Persist the non-secret S3 config to the vault and the "
            "access-key + secret-key to the OS keyring. Interactive by "
            "default; pass all flags for non-interactive setup."
        ),
    )
    _add_vault_args(setup_parser)
    setup_parser.add_argument("--endpoint", default=None, help="S3 endpoint URL")
    setup_parser.add_argument("--bucket", default=None, help="bucket name")
    setup_parser.add_argument("--region", default=None, help="signing region")
    setup_parser.add_argument("--prefix", default="", help="optional path prefix under the bucket")
    setup_parser.add_argument("--access-key", default=None, help="AWS access key id")
    setup_parser.add_argument("--secret-key", default=None, help="AWS secret access key")
    setup_parser.set_defaults(handler=_cmd_setup)

    status_parser = sync_subparsers.add_parser(
        "status",
        help="report sync configuration + reachability + watermarks",
    )
    _add_vault_args(status_parser)
    status_parser.set_defaults(handler=_cmd_status)

    push_parser = sync_subparsers.add_parser(
        "push",
        help="push the current vault snapshot to the configured store",
    )
    _add_vault_args(push_parser)
    push_parser.set_defaults(handler=_cmd_push)

    pull_parser = sync_subparsers.add_parser(
        "pull",
        help="pull the latest snapshot from the configured store",
    )
    _add_vault_args(pull_parser)
    pull_parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help=(
            "restore-to path (default: overwrite the configured vault); "
            "the target's salt sidecar must already exist"
        ),
    )
    pull_parser.set_defaults(handler=_cmd_pull)

    parser.set_defaults(handler=_print_help_when_no_subcommand(parser))


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; falls back to $TESSERA_PASSPHRASE / keyring",
    )


def _print_help_when_no_subcommand(
    parser: argparse.ArgumentParser,
) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _handler


def _cmd_setup(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        warn(str(exc))
        return 2
    if not vault_path.exists():
        warn(f"vault not found at {vault_path}; run `tessera init` first")
        return 2

    endpoint = args.endpoint or _prompt("S3 endpoint URL")
    bucket = args.bucket or _prompt("bucket name")
    region = args.region or _prompt("signing region (e.g. us-east-1)")
    prefix = (
        args.prefix
        if args.prefix is not None
        else _prompt("optional prefix (blank for none)", allow_empty=True)
    )
    access_key = args.access_key or _prompt("AWS access key id")
    secret_key = args.secret_key or _prompt_secret("AWS secret access key")

    if not endpoint or not bucket or not region or not access_key or not secret_key:
        warn("setup aborted: endpoint, bucket, region, access-key, secret-key required")
        return 2

    stored = sync_config.StoredConfig(
        endpoint=endpoint,
        bucket=bucket,
        region=region,
        prefix=prefix,
    )

    try:
        with open_vault(vault_path, passphrase) as vc:
            sync_config.save_config(vc.connection, config=stored)
    except CliError as exc:
        return fail(str(exc))

    try:
        sync_config.save_credentials(
            stored=stored,
            access_key_id=access_key,
            secret_access_key=secret_key,
        )
    except keyring_cache.KeyringUnavailableError as exc:
        return fail(f"keyring write failed: {exc}")

    sid = store_identity(endpoint=endpoint, bucket=bucket, prefix=prefix)
    success(f"sync configured: {bucket}@{endpoint} (store_id={sid})")
    info("run `tessera sync status` to verify reachability")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        warn(str(exc))
        return 2
    if not vault_path.exists():
        warn(f"vault not found at {vault_path}")
        return 2

    try:
        with open_vault(vault_path, passphrase) as vc:
            try:
                stored = sync_config.load_config(vc.connection)
            except sync_config.SyncNotConfiguredError as exc:
                warn(str(exc))
                return 2
            sid = store_identity(
                endpoint=stored.endpoint,
                bucket=stored.bucket,
                prefix=stored.prefix,
            )
            try:
                watermark = read_watermark(vc.connection, store_id=sid)
            except CorruptWatermarkError as exc:
                return fail(str(exc))
    except CliError as exc:
        return fail(str(exc))

    info(f"endpoint: {stored.endpoint}")
    info(f"bucket:   {stored.bucket}")
    info(f"region:   {stored.region}")
    info(f"prefix:   {stored.prefix or '(root)'}")
    info(f"store_id: {sid}")
    info(f"local watermark: {watermark}")

    # Reachability + latest-manifest probe goes through the S3
    # adapter; credentials must be loaded for this. A missing
    # keyring entry is reportable but not fatal — the operator
    # might be running ``status`` from a host that doesn't have
    # the credentials yet (e.g., before re-running setup on a
    # restored host).
    try:
        access_key, secret_key = sync_config.load_credentials(stored=stored)
    except sync_config.SyncCredentialsMissingError as exc:
        warn(str(exc))
        info("reachability skipped (no credentials)")
        return 0
    s3_config = sync_config.assemble_s3_config(
        stored=stored,
        access_key_id=access_key,
        secret_access_key=secret_key,
    )
    with S3BlobStore(s3_config) as store:
        try:
            store.initialize()
        except S3BucketUnreachableError as exc:
            warn(f"bucket unreachable: {exc}")
            return 1
        latest = store.latest_manifest_sequence()

    if latest is None:
        info("store reachable; no manifests yet (no push has happened)")
    else:
        info(f"store reachable; latest manifest sequence: {latest}")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        warn(str(exc))
        return 2
    if not vault_path.exists():
        warn(f"vault not found at {vault_path}")
        return 2

    # Derive the key once and reuse it both as the SQLCipher
    # unlock key and the AES-GCM master key. VaultConnection
    # does not expose the key after open, so we own the key
    # locally for the duration of the push.
    try:
        salt = load_salt(vault_path)
    except FileNotFoundError as exc:
        return fail(str(exc))

    try:
        with derive_key(passphrase, salt) as key, VaultConnection.open(vault_path, key) as vc:
            stored = _load_or_complain(vc.connection)
            if stored is None:
                return 2
            try:
                access_key, secret_key = sync_config.load_credentials(stored=stored)
            except sync_config.SyncCredentialsMissingError as exc:
                return fail(str(exc))
            s3_config = sync_config.assemble_s3_config(
                stored=stored,
                access_key_id=access_key,
                secret_access_key=secret_key,
            )
            master_key_bytes = bytes.fromhex(key.hex())
            with S3BlobStore(s3_config) as store:
                try:
                    result = push(
                        vault_path=vault_path,
                        conn=vc.connection,
                        store=store,
                        master_key=master_key_bytes,
                    )
                except PushChainBreakError as exc:
                    return fail(f"audit chain broken; refusing push: {exc}")
                except (PushError, S3BlobStoreError) as exc:
                    return fail(f"push failed: {exc}")
    except CliError as exc:
        return fail(str(exc))

    success(
        f"pushed sequence {result.sequence_number} ({result.bytes_uploaded} bytes); "
        f"audit_chain_head={result.audit_chain_head[:16]}…"
    )
    return 0


def _cmd_pull(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        warn(str(exc))
        return 2

    target_path = args.target.expanduser() if args.target is not None else vault_path
    if not vault_path.exists():
        warn(f"vault not found at {vault_path}; cannot read sync config or watermark")
        return 2

    try:
        salt = load_salt(vault_path)
    except FileNotFoundError as exc:
        return fail(str(exc))

    try:
        with derive_key(passphrase, salt) as key, VaultConnection.open(vault_path, key) as vc:
            stored = _load_or_complain(vc.connection)
            if stored is None:
                return 2
            try:
                access_key, secret_key = sync_config.load_credentials(stored=stored)
            except sync_config.SyncCredentialsMissingError as exc:
                return fail(str(exc))
            s3_config = sync_config.assemble_s3_config(
                stored=stored,
                access_key_id=access_key,
                secret_access_key=secret_key,
            )
            sid = store_identity(
                endpoint=stored.endpoint,
                bucket=stored.bucket,
                prefix=stored.prefix,
            )
            try:
                watermark = read_watermark(vc.connection, store_id=sid)
            except CorruptWatermarkError as exc:
                return fail(str(exc))
            master_key_bytes = bytes.fromhex(key.hex())
            with S3BlobStore(s3_config) as store:
                try:
                    result = pull(
                        store=store,
                        target_path=target_path,
                        master_key=master_key_bytes,
                        last_restored_sequence=watermark,
                    )
                except (PullError, S3BlobStoreError) as exc:
                    return fail(f"pull failed: {exc}")
            # Update watermark only when the target is the
            # configured vault. A --target restore-to-different-
            # location flow is a one-shot read; persisting that
            # sequence as the local watermark would block the
            # next pull against the configured vault.
            if args.target is None:
                write_watermark(vc.connection, store_id=sid, sequence=result.sequence_number)
    except CliError as exc:
        return fail(str(exc))

    success(
        f"pulled sequence {result.sequence_number} ({result.bytes_written} bytes) to {target_path}"
    )
    return 0


def _load_or_complain(conn: sqlcipher3.Connection) -> sync_config.StoredConfig | None:
    try:
        return sync_config.load_config(conn)
    except sync_config.SyncNotConfiguredError as exc:
        warn(str(exc))
        return None


def _prompt(question: str, *, allow_empty: bool = False) -> str:
    answer = input(f"{question}: ").strip()
    if not answer and not allow_empty:
        return ""
    return answer


def _prompt_secret(question: str) -> str:
    return getpass.getpass(f"{question}: ").strip()


__all__ = ["register"]
