# Garmin Connect MCP Server

![Garmin Connect MCP Server](docs/heading.png)

A Model Context Protocol (MCP) server for Garmin Connect integration. Access your activities, health data, training metrics, and more through Claude and other LLMs.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/garmin-connect-mcp.svg)](https://pypi.org/project/garmin-connect-mcp/)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue.svg)](https://github.com/eddmann/garmin-connect-mcp/pkgs/container/garmin-connect-mcp)

## Overview

This MCP server provides 26 tools to interact with your Garmin Connect account, organized into 9 categories:

- Activities (3 tools) - Query activities and view detailed metrics
- Analysis (2 tools) - Compare activities and find similar workouts
- Health & Wellness (4 tools) - Access health metrics, sleep, heart rate, and activity data
- Training (3 tools) - Analyze training periods and performance trends
- User Profile (1 tool) - Access profile, statistics, and personal records
- Challenges & Goals (2 tools) - Track goals, PRs, badges, and challenges
- Devices & Gear (2 tools) - Manage devices and equipment
- Weight Management (3 tools) - Query, add, or explicitly delete weight data
- Other (6 tools) - Workouts, capability-scoped health logging, women's health tracking

Additionally, the server provides:

- 3 MCP Resources - Athlete profile, training readiness, and daily health for ongoing context
- 6 MCP Prompts - Templates for common queries (training analysis, sleep quality, readiness checks, activity analysis, run comparison, health summary)

## Prerequisites

- [uv](https://github.com/astral-sh/uv) (the package requires Python 3.12+, which uv can manage), OR
- Docker

## Installation & Setup

### How Authentication Works

1. Bootstrap Login - The `auth` command uses your email, password, and optional MFA code once
2. Ephemeral Credentials - Account credentials stay in process memory and are never saved
3. Canonical Storage - One OAuth token store is saved under `~/.garminconnect/`
4. In-memory Runtime - The dependency never receives the canonical path and cannot truncate it directly
5. Atomic Refresh - Refreshed tokens use an inter-process lock, generation check, protected staging, and atomic replace
6. Token-only Runtime - The MCP server never falls back to a saved password or interactive login
7. Persistence - Host installs reuse the token store; Docker requires a read-write volume mount

### Option 1: Using uvx

```bash
uvx garmin-connect-mcp auth
```

This will prompt for your credentials, complete Garmin authentication, and save OAuth tokens
for the MCP server to reuse under `~/.garminconnect/`. Password and MFA values are not saved.

To use a custom token directory, set `GARMINTOKENS` in the process environment:

```bash
GARMINTOKENS=/secure/path/to/garmin-tokens
```

`GARMINTOKENS` must name a dedicated directory, not a project, home, filesystem root, or current
working directory. Its parent must already exist, and every entry in the parent chain must be
protected against replacement by another principal; the application creates only the final
dedicated child. An existing directory must already be owner-only and contain no unrelated
entries. Ordinary login/token refresh never changes protection on a pre-existing directory.

The server does not load dotenv files. Older `~/.garminconnect.env` or local `.env` files are
inspected only by the explicit `auth doctor` and `auth migrate` commands. On POSIX the token
directory/file must be owned by the current user with modes `0700`/`0600`; on Windows the
application installs and verifies protected ACLs for the current user and SYSTEM. Redirected
paths (symlinks, and Windows reparse points such as junctions) are rejected.

### Option 2: Using Docker

```bash
# Pull the image
docker pull ghcr.io/eddmann/garmin-connect-mcp:latest
```

Create a Docker-managed parent volume, then run the interactive bootstrap login. The token store
is an initially absent child so the application can create and own it safely:

```bash
docker volume create garmin-connect-mcp-tokens

docker run -it --rm \
  -v "garmin-connect-mcp-tokens:/root/.garmin-connect-mcp" \
  -e "GARMINTOKENS=/root/.garmin-connect-mcp/tokens" \
  ghcr.io/eddmann/garmin-connect-mcp:latest \
  auth
```

Use `-it` so password and MFA prompts work. The same volume must be mounted read-write
when the server runs so refreshed tokens can be persisted. The process can start without a
token, but every token-dependent tool or resource call then fails with an instruction to run
`garmin-connect-mcp auth`; it never falls back to a password.

The named volume is intentional: Docker creates its root for the container's root user, while the
application creates the `tokens` child with owner-only protection. A host bind mount normally
retains the host UID and is rejected when the container runs as root. Advanced Linux users who
need a bind mount must run the container with the host UID/GID and set `GARMINTOKENS` to an
appropriate dedicated child directory explicitly.

### Auditing and Migrating Older Installs

Older versions could leave a second token copy at `~/.garminconnect_base64` and save Garmin
credentials in dotenv files. Inspect the installation without displaying secret values:

```bash
uvx garmin-connect-mcp auth doctor
```

If `doctor` reports an interrupted token write, the protected staging artifact is preserved:
read-only startup, diagnostics, and migration planning never delete or promote an ambiguous token
generation. A still-usable canonical token remains the runtime source. Complete a fresh `auth` login
or a successful token refresh before migration; only a writer that has already fsynced another valid,
protected candidate may remove the interrupted artifact. Do not copy, decode, or manually merge it.
If the artifact itself is no longer owner-only, `doctor` treats that as possible disclosure rather
than a recoverable protected state; re-authenticate and rotate the affected Garmin session.

An interrupted dotenv migration is handled separately. Each known dotenv target has one
deterministic transaction sidecar, which `doctor` reports without changing it. `auth migrate`
removes that sidecar only after confirmation and only while holding the global migration lock,
after revalidating that it is an owner-controlled, single-link file with exactly the target's
protection and either empty or the recognized original/cleaned counterpart. Unknown or changed
sidecar contents are refused and are never removed by a filename glob.

After a valid canonical token exists, print the exact migration plan and apply it with an
explicit confirmation:

```bash
uvx garmin-connect-mcp auth migrate
```

The plan names every target path and dotenv key without printing values. Stored credentials
are removed only after the migration lock is held and the complete positive and negative dotenv,
legacy-token, quarantine, and recovery inventory still matches the prepared plan. On POSIX, cleanup
preserves the exact owning user, owning group, mode, and supported metadata; it refuses a parent
path another user could rewrite, a group the process cannot reproduce, or a foreign-owned
artifact instead of trying to repair it. A recognized legacy token is marked as a possible prior
disclosure unless it is already owner-only, then hardened and atomically moved into an owner-only
quarantine inside the canonical store so the operation remains recoverable. An existing
current-owned quarantine with weaker protection is repaired only as an explicit item in the
confirmed migration plan. A foreign-owned quarantine, any post-plan owner/protection change, or a
quarantine that is not owner-only immediately before purge is refused. Verify normal
authentication, then perform the separately planned deletion:

```bash
uvx garmin-connect-mcp auth migrate --purge
```

`--yes` skips the interactive confirmation but does not broaden scope. A local project `.env`
requires `--include-local-env`; a non-default legacy-token path requires
`--allow-custom-legacy`. Unknown files, changed plans, redirected paths, cross-filesystem moves,
unreadable, oversized, or non-UTF-8 dotenv files, token-shaped files that the installed
`garminconnect` cannot load, and POSIX dotenv files with extended ACLs or attributes that cannot
be preserved are refused. Legacy setting names are matched case-insensitively, including their
original spelling in the displayed cleanup plan.

If an older dedicated token directory has unsafe permissions, `auth migrate` includes the repair
in its displayed confirmation plan. Permission changes are durably synchronized on POSIX and the
canonical generation is revalidated afterward. First creation durably synchronizes both the new
directory inode and its parent entry on POSIX. Ordinary `auth` refuses to repair or claim the
directory implicitly.

If `doctor` reports legacy variables supplied by an MCP launcher or parent process, remove
`GARMIN_EMAIL`, `GARMIN_PASSWORD`, and `GARMINTOKENS_BASE64` from that external configuration;
the migration command cannot mutate its parent environment.

If an older dotenv file contains a custom `GARMINTOKENS` path, `doctor/migrate` will use it to
locate the canonical token, but the MCP runtime will not. Move that setting into the MCP
launcher's process environment before restarting the server.

If a token or password may have been readable by another user, treat it as compromised and
re-authenticate before migration. Permission repair or deleting a local copy cannot undo
earlier disclosure.

Every in-memory Garmin wrapper is bound to one canonical token fingerprint. The runtime verifies
that generation before each remote call and conditionally revalidates or persists it afterward.
An external re-authentication permanently revokes older wrappers, so a multi-call operation cannot
continue against a stale account generation.

### Write Capabilities

The server is read-only by default. Read tools receive a method-restricted client facade, and a
normal server start cannot call any Garmin mutation method. Mutation previews run locally without
loading Garmin tokens. To execute a write, the host operator must set `GARMIN_WRITE_CAPABILITIES`
before starting the server to a comma-separated list containing only the required capabilities:

| Capability                | Permitted operation          |
| ------------------------- | ---------------------------- |
| `weight.write`            | Add a weight entry           |
| `weight.delete`           | Delete selected weigh-ins    |
| `workouts.upload`         | Upload a workout             |
| `health.body_composition` | Log body-composition data    |
| `health.blood_pressure`   | Log blood-pressure data      |
| `health.hydration`        | Log hydration data           |

For example, enable weight addition and hydration logging without granting deletion or workout
upload:

```bash
GARMIN_WRITE_CAPABILITIES=weight.write,health.hydration uvx garmin-connect-mcp
```

Unknown capability names fail server startup. Enabling one capability never enables another, and
the value is read once when the server process starts. Restart the host after changing it. Do not
place credentials or health values in this setting.

Every mutation tool defaults to `dry_run=true`, validates input bounds, and returns a preview
without contacting Garmin. Execution requires all of the following:

1. The exact capability enabled by the operator.
2. `dry_run=false` in the tool call.
3. A caller-provided `idempotency_key` of 8-128 safe characters.

Weight deletion additionally requires the exact `required_confirmation` returned by a dry-run for
the selected IDs and is advertised to MCP clients as destructive. Inputs have conservative size,
range, count, and JSON-depth limits before any authenticated client is requested.

The server never automatically retries writes. A protected ledger in the dedicated token directory
stores only capability/key hashes, input fingerprints, and outcome states—never mutation inputs,
health values, or Garmin responses. Replaying the same capability, idempotency key, and inputs is
deduplicated across process restarts without another Garmin call; reusing a key with different
inputs is rejected. Confirmed and ambiguous records are never evicted to make room: when the
1,024-record safety bound is reached, new writes fail closed until an operator intentionally
archives the installation and starts with a fresh dedicated store.

Mutation transactions are serialized across server processes until Garmin's outcome is durably
recorded. A concurrent caller waits; if that wait times out, it must wait longer and retry the
same request with the same idempotency key, never a new key. An `in_progress` record can therefore
be observed only after its former lock owner exited without finalizing and is quarantined as an
abandoned, potentially committed operation.

If a response is lost or another genuinely ambiguous transport error occurs, that key is
quarantined and the response tells the caller which read tool to use for reconciliation instead of
advising a blind retry. Definite validation, authorization, and other pre-dispatch failures release
their reservation so a corrected request can safely reuse the key. Audit logs record capability,
method, an idempotency-key hash, and outcome, but not mutation inputs or health values.

## Claude Desktop Configuration

Add to your configuration file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

### Using uvx

After running `uvx garmin-connect-mcp auth`, configure Claude Desktop to start the
published package:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-connect-mcp"],
      "env": {
        "GARMIN_WRITE_CAPABILITIES": ""
      }
    }
  }
}
```

Keep the value empty for read-only operation. Replace it with the minimal comma-separated list
from the capability table only when writes are intentionally enabled.

### Using Local Source

For development, run from a local checkout:

```bash
cd garmin-connect-mcp
uv sync
uv run garmin-connect-mcp auth
```

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/ABSOLUTE/PATH/TO/garmin-connect-mcp",
        "garmin-connect-mcp"
      ]
    }
  }
}
```

### Using Docker

```json
{
  "mcpServers": {
    "garmin": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-v",
        "garmin-connect-mcp-tokens:/root/.garmin-connect-mcp",
        "-e",
        "GARMINTOKENS=/root/.garmin-connect-mcp/tokens",
        "-e",
        "GARMIN_WRITE_CAPABILITIES=",
        "ghcr.io/eddmann/garmin-connect-mcp:latest"
      ]
    }
  }
}
```

Create and authenticate the `garmin-connect-mcp-tokens` named volume with the Docker bootstrap
command above before starting Claude Desktop. Reuse that exact volume name in both commands.
The empty write-capability value is explicit read-only mode; replace it only with the minimal list
needed by that host.

## Usage

Ask Claude to interact with your Garmin data using natural language. The server provides tools, resources, and prompt templates to help you get started.

### Quick Start with MCP Prompts

Use built-in prompt templates for common queries (available via prompt suggestions in Claude):

- `analyze_recent_training` - Analyze my training over the past 30 days
- `sleep_quality_report` - Analyze sleep quality with recommendations
- `training_readiness_check` - Check if I'm ready to train hard today
- `activity_deep_dive` - Deep dive into a specific activity
- `compare_recent_runs` - Compare recent runs to track progress
- `health_summary` - Show comprehensive health overview

### Activities

```
"Show me my runs from the last 30 days"
"Get details for my half marathon yesterday including splits and heart rate zones"
"Show me the comments on my latest cycling activity"
```

### Training Analysis

```
"Analyze my training over the past 30 days"
"Compare my last three 10K runs"
"Find runs similar to my tempo workout from last week"
```

### Health & Wellness

```
"How did I sleep last night?"
"What's my Body Battery level today?"
"Show me my stress levels and recovery status"
"Am I ready to train hard today?"
```

_Note: The athlete profile resource (`garmin://athlete/profile`) and daily health resource (`garmin://health/today`) automatically provide ongoing context._

### Performance Metrics

```
"What's my VO2 max trend?"
"Show me my training readiness and recent stats"
```

_Note: List-returning tools use cursor-based pagination with default limits (20 items for activities, 7 days for health data)._

### Request resource budgets

Every tool and resource has a fixed server-side resource policy. Date ranges are inclusive and
are validated before the authenticated Garmin client is requested. Future dates are allowed for
compatibility, but count toward the same limit. Relative dates use the server's local timezone.

| Surface | Inclusive range | Page / item ceiling | Calls | Response | Deadline | Partial |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `query_activities` | 366 days | 20 / 50 | 25 | 1 MiB | 30 s | Yes |
| `get_activity_details` | N/A | 50 projected collection items | 7 | 1 MiB | 30 s | No |
| `get_activity_social` | N/A | N/A | 1 | 1 MiB | 30 s | No |
| `compare_activities` | N/A | 5 input activities | 5 | 1 MiB | 30 s | No |
| `find_similar_activities` | N/A | 10 / 20 results | 2 | 1 MiB | 30 s | No |
| `query_health_summary` | 366 days | 7 / 30 days | 100 | 1 MiB | 30 s | Yes |
| `query_sleep_data` | 31 days | 7 / 31 days | 40 | 1 MiB | 30 s | Yes |
| `query_heart_rate_data` | 31 days | 7 / 31 days | 70 | 1 MiB | 30 s | Yes |
| `query_activity_metrics` | 31 days | 7 / 31 days; 1 day with range metrics | 100 | 1 MiB | 30 s | Yes |
| `query_devices` | 31 solar days | 31 projected collection items | 8 | 1 MiB | 30 s | No |
| `query_gear` | N/A | 50 projected collection items | 25 | 1 MiB | 30 s | No |
| `get_user_profile` | N/A | 50 projected collection items | 8 | 1 MiB | 30 s | No |
| `query_goals_and_records` | N/A | 50 projected collection items | 2 | 1 MiB | 30 s | No |
| `query_challenges` | N/A | 50/category; 250 aggregate | 10 | 1 MiB | 30 s | No |
| `analyze_training_period` | 366 days | 54 weekly; 108 aggregate | 25 | 1 MiB | 30 s | No |
| `get_performance_metrics` | 366 days | 2,000 projected collection items | 10 | 1 MiB | 30 s | No |
| `get_training_effect` | 366 days | 366 projected collection items | 5 | 1 MiB | 30 s | No |
| `query_weight_data` | 366 days | 366 projected collection items | 5 | 1 MiB | 30 s | No |
| `add_weight_entry` | N/A | N/A | 1 | 64 KiB | 30 s | No |
| `delete_weight_entries` | N/A | 10 weigh-ins | 11 | 64 KiB | 30 s | No |
| `query_workouts` | N/A | 20 / 50 workouts | 2 | 1 MiB | 30 s | Yes |
| `upload_workout` | N/A | N/A | 1 | 64 KiB | 30 s | No |
| `log_body_composition` | N/A | N/A | 1 | 64 KiB | 30 s | No |
| `log_blood_pressure` | N/A | N/A | 1 | 64 KiB | 30 s | No |
| `log_hydration` | N/A | N/A | 1 | 64 KiB | 30 s | No |
| `query_womens_health` | 366 menstrual days | 366 projected collection items | 5 | 1 MiB | 30 s | No |
| `garmin://athlete/profile` | N/A | 50 projected collection items | 4 | 1 MiB | 30 s | No |
| `garmin://training/readiness` | N/A | 50 projected collection items | 2 | 1 MiB | 30 s | No |
| `garmin://health/today` | N/A | 50 projected collection items | 2 | 1 MiB | 30 s | No |

The call budget is a hard ceiling, not a promise that every option combination can fill the
maximum page. For example, a health-summary day can require up to six Garmin calls, so an
expensive 30-day page is rejected before its first call. All read responses have a 1 MiB
serialized UTF-8 limit and a 30-second request deadline. Write responses have a 64 KiB limit and
normally one remote-call budget. Date-wide weight deletion is expanded into one bounded lookup
and at most ten direct deletes; it refuses a larger date before the first delete. The pinned
Garmin dependency applies a 15-second timeout to each HTTP
attempt. Blocking reads run in a dedicated pool capped at eight workers, and writes use a
separate pool capped at two workers; queue wait counts toward the same request deadline.
Cancellation or the MCP deadline prevents any subsequent Garmin read even though Python cannot
forcibly stop an already running worker thread. If a submitted write does not confirm before
cancellation or the deadline, its Garmin outcome must be treated as unknown and requires
reconciliation before a retry with the same `idempotency_key`; external task cancellation itself
continues to propagate to the MCP host.

`query_activity_metrics` uses one-day pages when `blood_pressure` or
`body_composition` is requested for a range. Those Garmin methods return an aggregate window;
the one-day page guarantees that the range payload, metadata, and cursor cover the same dates.
Pass `limit=1` on those requests, or omit `limit` and reuse the returned cursor.

`garminconnect==0.3.6` internally auto-pages goals until an empty response and exposes earned
badges only as an unbounded collection. Those two sub-capabilities are returned as unavailable
instead of bypassing the MCP call/item budgets. Other challenge categories and workout lists use
bounded cursor pages.

The pinned adhoc-challenge method exposes historical challenges rather than an active or
available feed. `query_challenges` therefore calls it only for `status="all"`; for narrower
statuses the `adhoc_challenges` category is explicitly reported as unavailable.

Challenge categories use synchronized ordinary cursor pages: `has_more` and `cursor` mean that
another page exists, not that the current page is an error-tolerant partial result. This is logged
as `continuation=true`, separately from truncation. If the synchronized category payload cannot
fit the byte ceiling, the request fails closed instead of inventing a cursor that could skip a
category.

Some dependency methods return an entire range in one unpaged call. For device collections and
solar data, performance metrics, training progress, weight, and women's-health data, the date
ceiling bounds the upstream query and the projected public collections are then checked against
the table's aggregate item ceiling. An oversized collection fails closed with
`ITEM_BUDGET_EXCEEDED`; there is no continuation cursor because the pinned dependency exposes no
bounded page for those methods.

There are intentionally no environment-variable overrides for resource budgets. A host operator
cannot raise these public ceilings without changing, reviewing, and testing the policy registry.

Paginated responses use an opaque, versioned cursor bound to the exact surface, filters, page
size, and policy version. Reuse the original arguments with the returned cursor; changing a
filter or page size returns `INVALID_CONTINUATION_CURSOR`. The cursor is strictly validated but
unsigned because modifying it cannot raise any server-side range, item, call, deadline, or byte
limit.

When a surface whose table entry permits partial results stops at a normal page, scan-call, or
byte boundary, pagination contains
`partial=true`, a safe `truncation_reason`, the returned item count, and a cursor to the first
unreturned item. If one item cannot fit by itself, the response is
`RESPONSE_BUDGET_EXCEEDED`. Authentication, rate-limit, dependency, deadline, and cancellation
failures return a stable error rather than mixing an error with partial health data. The final
middleware guard replaces any oversized tool payload with the same bounded error.
After Garmin confirms a mutation, a late byte overflow or other response-delivery failure instead
returns a compact confirmed-success acknowledgement; it never replaces the confirmed write with a
retry-facing error.

Activity pagination uses Garmin's offset feed, which has no snapshot token. Its cursor fixes the
query end and all filters, but consistency remains weak if the upstream feed changes between
pages: newly inserted or deleted activities can shift later offsets. Consumers that require a
stable historical snapshot should complete pages without delay and de-duplicate by activity ID.

Common budget errors are `INVALID_DATE_RANGE`, `RANGE_TOO_LARGE`, `INVALID_PAGE_SIZE`,
`API_CALL_BUDGET_EXCEEDED`, `ITEM_BUDGET_EXCEEDED`, `RESPONSE_BUDGET_EXCEEDED`,
`REQUEST_DEADLINE_EXCEEDED`, and `INVALID_CONTINUATION_CURSOR`. Error payloads contain no Garmin
response body, health value, device identifier, credential, or raw exception text.

Example request shapes:

```text
query_activities(start_date="2026-06-01", end_date="2026-06-30", limit=20)
query_sleep_data(start_date="2026-06-01", end_date="2026-07-01", limit=7)
query_health_summary(start_date="2025-07-20", end_date="2026-07-19", limit=7)
```

If `pagination.has_more` is true, repeat the same call with the returned `pagination.cursor`.

### Response privacy

All tool and resource responses use a fail-closed public projection. Data that
was explicitly requested remains available, while unknown Garmin fields,
unrelated identifiers, device serial numbers, owner/profile IDs, and raw
internal exceptions are removed. Successful and error responses declare
`metadata.response_schema` as `"2"`.

Exact activity location is excluded by default. Pass `include_location=true`
to `query_activities` or `get_activity_details` only when coordinates are
needed. Pregnancy and menstrual data is returned only by an explicit
`query_womens_health` call; that call itself is the opt-in for the selected
category.

The MCP host and model receive the projected data and may retain it according
to the host's chat, logging, and cloud-storage policies. Read-only operation
does not make health data non-sensitive. There is no generic raw-response mode.
See the [complete response exposure and migration contract](docs/data-exposure.md).

## Available Tools

### Garmin API compatibility policy

The MCP surface is verified against exactly `garminconnect==0.3.6`. The runtime dependency and
the executable contract must be upgraded together: a version change is accepted only after every
registered tool and resource has a binding-compatible method signature and declared return shape,
all offline contract tests pass, and the matrix is updated. Relative dates (`today` and `yesterday`)
use the server process's local timezone and are converted to `YYYY-MM-DD` before calling Garmin.

See [the complete compatibility matrix](docs/garminconnect-compatibility.md) for every registered
surface, dependency method, unsupported capability, date rule, and binary response rule.

### Activities (3 tools)

| Tool                   | Description                                                            |
| ---------------------- | ---------------------------------------------------------------------- |
| `query_activities`     | Query projected activities; exact location requires `include_location=true` |
| `get_activity_details` | Get projected splits, weather, HR zones, and gear; location is opt-in  |
| `get_activity_social`  | Stable capability error: social calls are unavailable in 0.3.6         |

### Analysis (2 tools)

| Tool                      | Description                                     |
| ------------------------- | ----------------------------------------------- |
| `compare_activities`      | Compare 2-5 activities side-by-side             |
| `find_similar_activities` | Find activities similar to a reference activity |

### Health & Wellness (4 tools)

| Tool                     | Description                                                                   |
| ------------------------ | ----------------------------------------------------------------------------- |
| `query_health_summary`   | Query daily health summaries with pagination (stats, readiness, Body Battery) |
| `query_sleep_data`       | Query sleep data with stages, scores, and HRV                                 |
| `query_heart_rate_data`  | Query heart rate data with resting HR                                         |
| `query_activity_metrics` | Query activity metrics (steps, stress, respiration, SpO2, etc.)               |

### Training (3 tools)

| Tool                      | Description                                                         |
| ------------------------- | ------------------------------------------------------------------- |
| `analyze_training_period` | Analyze training over a time period with insights                   |
| `get_performance_metrics` | Get performance metrics (VO2 max, hill score, endurance, HRV, etc.) |
| `get_training_effect`     | Get training effect and progress summary                            |

### User Profile (1 tool)

| Tool               | Description                                          |
| ------------------ | ---------------------------------------------------- |
| `get_user_profile` | Get a projected athlete profile with stats and PRs   |

### Challenges & Goals (2 tools)

| Tool                      | Description                                         |
| ------------------------- | --------------------------------------------------- |
| `query_goals_and_records` | Query goals, personal records, and race predictions |
| `query_challenges`        | Query challenges and badges (by status and type)    |

### Devices & Gear (2 tools)

| Tool            | Description                                                  |
| --------------- | ------------------------------------------------------------ |
| `query_devices` | Query projected device data without serial, unit, or owner IDs |
| `query_gear`    | Query gear by profile number; stats additionally require a gear UUID |

### Weight Management (3 tools)

| Tool                    | Description                                                |
| ----------------------- | ---------------------------------------------------------- |
| `query_weight_data`     | Query weight data for date or range                        |
| `add_weight_entry`      | Preview or add one bounded entry with `weight.write`       |
| `delete_weight_entries` | Preview or confirm deletion of all weigh-ins on one date    |

### Other (6 tools)

| Tool                   | Description                                                        |
| ---------------------- | ------------------------------------------------------------------ |
| `query_workouts`       | List, get, or download workouts without mutation                   |
| `upload_workout`       | Validate, preview, or upload with `workouts.upload`                |
| `log_body_composition` | Validate, preview, or log with `health.body_composition`           |
| `log_blood_pressure`   | Validate systolic, diastolic, and pulse, then preview or log       |
| `log_hydration`        | Validate, preview, or log with `health.hydration`                  |
| `query_womens_health`  | Query pregnancy and menstrual cycle data                           |

## MCP Resources

Resources provide projected ongoing context to the LLM without requiring explicit tool calls.
They follow the same field allowlists as tools and never include location or reproductive-health data:

| Resource                      | Description                                        |
| ----------------------------- | -------------------------------------------------- |
| `garmin://athlete/profile`    | Compact athlete profile and projected daily health |
| `garmin://training/readiness` | Projected readiness and recovery context            |
| `garmin://health/today`       | Projected daily health snapshot                     |

## MCP Prompts

Prompt templates for common queries (accessible via prompt suggestion in Claude):

| Prompt                     | Description                                         |
| -------------------------- | --------------------------------------------------- |
| `analyze_recent_training`  | Analyze training over a specified period            |
| `sleep_quality_report`     | Sleep quality analysis with recommendations         |
| `training_readiness_check` | Check if ready to train hard today                  |
| `activity_deep_dive`       | Deep dive into a specific activity with all metrics |
| `compare_recent_runs`      | Compare recent runs to identify trends              |
| `health_summary`           | Comprehensive health overview                       |

## Development Quality Gate

Run the complete locked quality gate locally with:

```bash
uv run --locked scripts/qa.py
```

The gate includes linting, formatting, type checking, tests, and dependency auditing. Run only
the dependency audit with `make audit`. Temporary advisory exceptions belong in
`audit-exceptions.toml`; each entry must document reachability, an owner, a future expiry date,
and its removal condition. Release SBOMs can be generated with `make sbom`.

## License

MIT License - see [LICENSE](LICENSE) file for details

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by Garmin Ltd. or any of its affiliates. All product names, logos, and brands are property of their respective owners.
