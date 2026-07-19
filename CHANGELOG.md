# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Pin `garminconnect==0.3.6` to address CVE-2026-54447 and lock the verified in-memory
  token serialization contract; older versions could create a
  world-readable OAuth token store on multi-user Unix systems
- Persist exactly one canonical OAuth token store through an atomic application-owned writer
- Reject hard-linked secret artifacts; preserve interrupted token generations for diagnostics, and
  let only a writer with another protected, durable candidate remove them under the writer lock
- Keep runtime tokens in memory so dependency refreshes cannot truncate or overwrite the
  canonical file; generation checks reject stale cross-process writers
- Enforce owner-only POSIX modes or protected Windows ACLs and reject redirected token paths
- Require a non-broad dedicated token directory and never change an existing directory's
  protection outside an explicitly confirmed migration repair
- Preserve supported dotenv protection during one-shot atomic cleanup, refuse POSIX ACL/xattr
  files that cannot be preserved, and serialize complete migrations so stale rollback cannot
  restore removed credentials; migration temps receive and verify that protection before any
  retained secret bytes are written, while directory fsync makes dotenv cleanup and legacy
  quarantine/purge crash-durable on POSIX; deterministic transaction sidecars make interrupted
  dotenv cleanup or rollback visible to `auth doctor` and removable only by a confirmed,
  globally locked migration after exact content and protection validation; exact POSIX uid/gid/mode
  preservation and a complete positive/negative inventory revalidated under that lock prevent
  stale plans, while cross-platform integrity checks for the full pre-existing parent chain prevent
  group drift and pathname-swap attacks
- Make POSIX permission repair and first token-store creation crash-durable by synchronizing changed
  inodes and the new directory entry before reporting success
- Reject foreign-owned token stores, locks, and legacy artifacts before any chmod, write, or lock
  creation, including root processes handling host bind mounts
- Audit owner/mode/ACL protection for legacy and quarantined token copies, report broadly readable
  copies as possible prior disclosure, CAS their protection in migration plans, explicitly repair
  only a current-owned quarantine, and revalidate owner-only protection immediately before purge
- Keep successful Garmin mutations successful when later token persistence fails; invalidate
  and warn instead of encouraging a duplicate retry
- Keep password and MFA inputs ephemeral instead of saving Garmin credentials to dotenv files
- Authenticate the MCP runtime from tokens only, without automatic password fallback

### Added

- Add `auth doctor` to report legacy auth artifacts without exposing their contents
- Add a fingerprinted `auth migrate` plan that quarantines the deprecated token before the
  separately confirmed `auth migrate --purge` deletion

### Changed

- Share one synchronized Garmin session across MCP tools and resources
- Bind each in-memory wrapper to one canonical generation, revalidate it before and after remote
  calls, and permanently revoke stale wrappers after external authentication changes
- Use Windows shared-delete readers with `ReplaceFileW` so reads do not block atomic refreshes
- Validate the complete `garminconnect` 0.3 token schema before atomically replacing the
  canonical token
- Load runtime token configuration only from the process environment; dotenv files are migration-only
- Document Docker named volumes as the ownership-compatible persistence model

## [1.0.1] - 2026-05-19

### Changed

- Document published `uvx garmin-connect-mcp` usage as the primary setup path
- Simplify Claude Desktop configuration for published `uvx` usage
- Store interactive setup credentials in `~/.garminconnect.env` by default

## [1.0.0] - 2026-05-19

### Added

- Initial Garmin Connect MCP server release
- Garmin Connect activity, health, training, profile, device, gear, weight, workout, and women's health tools
- MCP resources for athlete profile, training readiness, and daily health context
- MCP prompts for training analysis, sleep quality, readiness checks, activity analysis, run comparison, and health summaries
- `garmin-connect-mcp` server entrypoint
- `garmin-connect-mcp auth` interactive authentication setup with MFA token persistence
- Docker image support via GitHub Container Registry

[1.0.1]: https://github.com/eddmann/garmin-connect-mcp/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/eddmann/garmin-connect-mcp/releases/tag/v1.0.0
