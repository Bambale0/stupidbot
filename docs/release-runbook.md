# Production release runbook

## Release invariant

A release is valid only when all evidence refers to the same full 40-character candidate SHA.

Required gates:

1. `CI` — compile, SQLite smoke and current-policy contracts;
2. `Release contracts` — Telegram UX, reusable references, admin and broad SQLite checks;
3. `Financial integrity` — PostgreSQL migrations, financial regression, transactional smoke and backup/restore;
4. `Staging rollout gate` — immutable archive, database/code backup, migration, restart, `/health`, full `/ready`, Mini App and package API;
5. `Release certification` — verifies the four evidence sources above and publishes a machine-readable certificate.

## Standard release

1. Merge small feature PRs into `dev` only after their feature gates are green.
2. Wait for the `Staging rollout gate` on the resulting `dev` SHA.
3. Confirm issue #17 contains `Staging rollout: passed` for the exact full SHA and GitHub job status `success`.
4. Open one release PR from `dev` to `main`.
5. Do not merge while the release PR is changing. Any new commit invalidates prior evidence and restarts certification.
6. Wait for:
   - `CI` success;
   - `Release contracts` success;
   - `Financial integrity` success;
   - `Release certification` success and its PR comment.
7. Inspect the `release-certificate-<SHA>` artifact. It must contain `status=certified`, the same candidate SHA and four passed evidence links.
8. Merge the release PR using a merge commit or squash according to repository policy.
9. Fast-forward `dev` to the resulting `main` release commit.
10. Verify `main...dev` has zero commits in both directions.

## Automatic certificate

`.github/workflows/release-certification.yml` runs on release PRs targeting `main` or `master`.

It waits for the latest exact-SHA runs of:

- `CI`;
- `Release contracts`;
- `Financial integrity`.

It then searches issue #17 for successful exact-SHA staging evidence. When all checks pass it:

- creates `release-evidence.json`;
- uploads it for 90 days;
- posts or updates a SHA-specific certification comment on the release PR.

The certificate contains no credentials, environment values, user data, prompts or provider payloads.

## Staging evidence

The staging artifact includes:

- immutable source archive checksum;
- rollout log;
- `staging-evidence.json`;
- exact candidate SHA;
- backup, migration, restart, restore, liveness, readiness, Mini App and package API results.

The rollout keeps rollback armed until all public checks pass.

## Rollback

If staging deployment fails after mutation starts, `ops/staging_rollout.sh` automatically restores the previous application files and restarts the service. The PostgreSQL dump is retained in the timestamped backup directory.

For a production rollback:

1. identify the last known-good release commit;
2. preserve the current code and database before any mutation;
3. restore code from the known-good immutable artifact;
4. restore the database only when the failed release included an incompatible data migration and the rollback decision explicitly requires it;
5. restart the service;
6. verify `/health`, `/ready`, Mini App and package API;
7. record the restored SHA and evidence in issue #17.

Never perform a database restore merely to fix an application-code error. Prefer a forward fix when the migrated schema remains backward compatible.

## Failure handling

- A failed or cancelled workflow does not count as evidence.
- A shortened SHA does not count as evidence.
- Staging evidence from another SHA does not count.
- A certificate generated before the latest PR synchronization is obsolete.
- `main` and `dev` must be synchronized after every production release.
