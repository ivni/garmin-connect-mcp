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

_Note: List-returning tools use cursor-based pagination with default limits (10 items for activities, 7 for health data)._

## Available Tools

### Activities (3 tools)

| Tool                   | Description                                                            |
| ---------------------- | ---------------------------------------------------------------------- |
| `query_activities`     | Query activities with pagination (by ID, date range, or specific date) |
| `get_activity_details` | Get comprehensive activity details (splits, weather, HR zones, gear)   |
| `get_activity_social`  | Get social details for an activity (likes, comments, kudos)            |

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
| `get_user_profile` | Get comprehensive athlete profile with stats and PRs |

### Challenges & Goals (2 tools)

| Tool                      | Description                                         |
| ------------------------- | --------------------------------------------------- |
| `query_goals_and_records` | Query goals, personal records, and race predictions |
| `query_challenges`        | Query challenges and badges (by status and type)    |

### Devices & Gear (2 tools)

| Tool            | Description                                                  |
| --------------- | ------------------------------------------------------------ |
| `query_devices` | Query device information (with settings, solar data, alarms) |
| `query_gear`    | Query gear and equipment (with defaults and usage stats)     |

### Weight Management (3 tools)

| Tool                    | Description                                                |
| ----------------------- | ---------------------------------------------------------- |
| `query_weight_data`     | Query weight data for date or range                        |
| `add_weight_entry`      | Preview or add one bounded entry with `weight.write`       |
| `delete_weight_entries` | Preview or confirm destructive deletion with `weight.delete` |

### Other (6 tools)

| Tool                   | Description                                                        |
| ---------------------- | ------------------------------------------------------------------ |
| `query_workouts`       | List, get, or download workouts without mutation                   |
| `upload_workout`       | Validate, preview, or upload with `workouts.upload`                |
| `log_body_composition` | Validate, preview, or log with `health.body_composition`           |
| `log_blood_pressure`   | Validate, preview, or log with `health.blood_pressure`             |
| `log_hydration`        | Validate, preview, or log with `health.hydration`                  |
| `query_womens_health`  | Query pregnancy and menstrual cycle data                           |

## MCP Resources

Resources provide ongoing context to the LLM without requiring explicit tool calls:

| Resource                      | Description                                        |
| ----------------------------- | -------------------------------------------------- |
| `garmin://athlete/profile`    | Athlete profile with stats, zones, and PRs         |
| `garmin://training/readiness` | Current training readiness and Body Battery        |
| `garmin://health/today`       | Today's health snapshot (steps, sleep, stress, HR) |

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
make qa
```

The gate includes linting, formatting, type checking, tests, and dependency auditing. Run only
the dependency audit with `make audit`. Temporary advisory exceptions belong in
`audit-exceptions.toml`; each entry must document reachability, an owner, a future expiry date,
and its removal condition. Release SBOMs can be generated with `make sbom`.

## License

MIT License - see [LICENSE](LICENSE) file for details

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by Garmin Ltd. or any of its affiliates. All product names, logos, and brands are property of their respective owners.
