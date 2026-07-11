# Automatic connector-token renewal

**Status:** Design approved; specification review pending

**Date:** 2026-07-11

## Decision

Tessera will proactively reissue 90-day service access tokens for every managed file-based MCP connector while `tesserad` is running. It will rewrite the relevant client configuration before the existing token expires, retain the existing token until its natural expiry, and never force a client restart or revoke the credential a running client is using.

The daemon cannot use the existing refresh-token flow for this purpose. MCP client configurations contain only the access token, not the paired raw refresh token, and service refresh tokens expire after seven days. A daemon-owned reissue-and-config-update flow is therefore required.

## Context

The service-token default is 24 hours. `tessera connect` can currently override this to 90 days, but it mints a static token and writes it to a client configuration. Static MCP clients cache that credential at process startup. When it expires, a healthy daemon rejects every capture and recall request until an operator reruns `tessera connect` and restarts the client.

The 2026-06-22 collection failure demonstrated the consequence: all connector credentials expired, so all capture and recall calls failed with 401 while the daemon remained live. Collection became operational only after manual reissuance on 2026-06-23.

## Goals

- Renew existing file-based connector configurations automatically before their access tokens expire.
- Manage Claude Desktop, Claude Code, Cursor, Codex, OpenCode, Oh My Pi, and Pi installations.
- Support an explicitly configured custom path from `tessera connect --path`.
- Preserve the original agent, client identity, and least-privilege scopes on every replacement capability.
- Keep running MCP clients functional until their next manual restart.
- Make file replacement atomic, preserve the existing backup behavior, and recover deterministically from daemon crashes between vault and filesystem updates.
- Surface configuration drift and renewal failures explicitly without silently recreating or overwriting a user-removed connector entry.

## Non-goals

- Starting the daemon after a reboot or logout. The operator continues to restart `tesserad` manually.
- Restarting, signalling, or otherwise controlling client applications.
- Managing ChatGPT Developer Mode, which has no file-based connector configuration.
- Replacing the existing session/service refresh-token API.
- Retaining raw access or refresh tokens in the vault beyond the existing salted hashes.

## Managed-installation registry

Schema version 5 adds a `managed_connector_installations` table. Each row represents one file-based Tessera configuration entry:

- `id`: stable installation identifier.
- `connector_id`: one supported file-based connector identifier.
- `config_path`: absolute path, unique across managed installations.
- `agent_id`: owning Tessera agent.
- `active_capability_id`: the capability currently expected in the file.
- `pending_capability_id`: a replacement staged for a filesystem update or crash recovery; otherwise null.
- `access_ttl_seconds`: the access-token lifetime used for replacements, initially 90 days.
- `created_at` and `updated_at`: UTC epoch timestamps.

The two capability references use foreign keys. A constraint prevents a row from naming the same capability as both active and pending. The table stores identifiers and paths only; it never stores raw token material.

`tessera connect <client>` continues to issue the credential and safely write the connector configuration. After that write succeeds, it creates or updates the registry entry for the exact connector and path. New managed file-based installations use a 90-day service token by default. Existing installations retain their present token until the renewal horizon, then enter the managed 90-day cadence.

On its first pass, the renewal worker may adopt existing default-path configurations only when all conditions hold:

1. The default configuration file exists and has a Tessera entry.
2. The entry resolves to a live local capability for the expected file-based client.
3. The capability is a service credential associated with a single agent.

The token-resolution helper used for adoption and crash recovery compares the raw file token against the stored salted hashes without updating `last_used_at`. It returns an identifier only; it does not expose or persist any raw credential. The worker never creates missing config files, and it never guesses a custom path. A custom-path configuration is managed when its owner reruns `tessera connect <client> --path <path>` once.

## Renewal lifecycle

The daemon supervisor gains a renewal task. It performs an immediate reconciliation pass after daemon startup and then runs every 24 hours. A managed installation is due when its active capability expires within 14 days.

For every due installation, the worker performs the following sequence:

1. Read the active capability and verify that the registered config still contains the expected active token. If the entry is absent, malformed, or manually changed, record drift and leave the file untouched.
2. Issue a new 90-day `service` capability with the same agent ID, client name, and scope as the active capability.
3. Persist the new capability ID in `pending_capability_id` before touching the file.
4. Use the existing connector `apply()` method to replace only Tessera's configuration entry. `write_safely()` retains the prior file as a timestamped backup and atomically replaces the file in the same directory.
5. Promote the pending capability to `active_capability_id`, clear `pending_capability_id`, and record the completed renewal.
6. Leave the former active capability unrevoked. It remains usable until its original expiry so running clients are never disconnected by a successful renewal.

The 14-day horizon gives an operator who starts the daemon manually after a reboot enough time to receive a completed renewal before the prior 90-day token expires. The immediate startup pass avoids waiting up to a day after a restart.

## Failure and crash recovery

Filesystem writes and vault transactions cannot form a single transaction. `pending_capability_id` is the durable recovery boundary.

- If the safe config replacement fails, the worker revokes the unused pending capability, clears the pending reference, records the failure, and leaves the previous configuration and active credential unchanged.
- If the daemon stops after staging the pending capability but before replacing the config, startup reconciliation sees the active token in the file, revokes the unused pending credential, and clears the pending reference.
- If the daemon stops after the config replacement but before promotion, startup reconciliation sees the pending token in the file and promotes it to active.
- If the file contains neither the active nor pending credential, reconciliation records configuration drift, leaves the file untouched, and does not mint another token.

A renewal failure is never treated as success. It emits a non-secret audit event and daemon diagnostic containing the connector ID, configuration path, installation ID, failure class, and retry eligibility. The next 24-hour pass retries only states that remain safe to retry.

## Audit and observability

Add audit operations for installation registration, renewal staging, renewal completion, renewal failure, and reconciliation. Allowed payloads contain IDs, connector name, path, expiry timestamps, failure class, and whether a backup was written; they never contain access tokens, refresh tokens, token hashes, or raw file contents.

Emit structured runtime events for successful renewal, configuration drift, and failure. The daemon log records a single-line failure diagnostic. `tessera tokens list` remains a capability-metadata inventory; a renewal-status command or section reports each managed installation's current expiry, next eligible renewal date, pending state, and most recent error without exposing a credential.

## Testing

The implementation must add deterministic tests with an injectable clock and temporary configuration files.

1. Registration records default and custom paths only after a successful connector write.
2. Adoption registers valid existing default-path entries and ignores absent, malformed, expired, or mismatched entries.
3. A due installation receives a replacement that preserves agent, client, scope, class, and 90-day lifetime.
4. An installation outside the 14-day horizon is not rewritten and does not mint a capability.
5. Every supported JSON, TOML, and Claude Desktop stdio connector format is rewritten through its existing connector implementation without losing unrelated configuration.
6. A replacement config uses the new token; the old token remains authorized until its original expiry.
7. A failed config write leaves the old config active, revokes the unused pending capability, and records the failure.
8. Restart reconciliation covers staged-only, file-written-but-unpromoted, and user-drifted states.
9. The supervisor starts, stops, and awaits the renewal task alongside its existing background tasks.
10. Audit payload assertions prove raw token material never enters the audit chain, events database, registry, or error output.

## Documentation changes

Update the connector and token-lifecycle documentation to state that file-based connectors are automatically renewed while the daemon runs, use 90-day managed service tokens, and retain old credentials until expiry. Document the operational boundary: after a reboot, starting the daemon resumes renewal, but a running client adopts a rewritten config only when the client is restarted.