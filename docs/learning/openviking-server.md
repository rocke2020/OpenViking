# OpenViking Server — Start Summary

> Quick-reference for running the local OpenViking server from this repo's
> editable `.venv`, for the self-learning loop (read source → tweak → restart
> → observe).

## TL;DR

The OV server is a launchd-managed job (`com.openviking.server`) that execs
`~/.openviking/bin/start-openviking.sh`, which in turn execs
`/Users/rocke_dong/codes/OpenViking/.venv/bin/openviking-server`. The `.venv`
is an **editable** install, so any change to `openviking/**.py` takes effect
on the next server restart. Two workflows cover everything you'll do:
**kickstart** for daily edit-restart cycles (binary is healthy), and
**unload / load** for maintenance windows where the binary might be temporarily
broken (git tag switch + `uv sync`).

There are three intentional OV command surfaces on this machine:
`~/bin/ov` points at the test server checkout, `~/codes/ov1/.venv/bin/ov` is
the active development CLI, and `/opt/homebrew/bin/ov` is the npm-installed
optional CLI, not a full server installation. Treat the Homebrew-path CLI as
**prod-sim CLI only**: useful for testing what a user gets from
`npm install -g @openviking/cli`, but call it by absolute path so it never
silently replaces the editable dev/test CLIs.

## Current Setup

| Component | Value |
|---|---|
| LaunchAgent label | `com.openviking.server` |
| Plist | `~/Library/LaunchAgents/com.openviking.server.plist` |
| Start script | `~/.openviking/bin/start-openviking.sh` |
| Binary | `/Users/rocke_dong/codes/OpenViking/.venv/bin/openviking-server` |
| Python env | repo `.venv` (editable; refresh with `uv sync` after changing tags/HEAD) |
| Host / port | `127.0.0.1:1933` |
| Config | `~/.openviking/ov.conf` (has `embedding.allow_metadata_override=true`) |
| Logs | `~/.openviking/data/log/openviking.log` (stdout), `openviking.err.log` (stderr) |
| RunAtLoad / KeepAlive | both `true` (auto-start on login, auto-restart on crash) |

OpenClaw (`oc`) gateway at `127.0.0.1:18789` connects to this server via the
`openviking` context-engine plugin (`plugins.entries.openviking.enabled=true`,
`config.baseUrl=http://127.0.0.1:1933`). `memory-tencentdb` is disabled.

## CLI Environments

The default `ov` command should follow the running test server, while dev and
prod-sim stay available through explicit paths. This prevents a stale conda or
Homebrew install from accidentally controlling the local test service.

| Role | Command | Intended use |
|---|---|---|
| Test default | `ov` → `~/bin/ov` → `/Users/rocke_dong/codes/OpenViking/.venv/bin/ov` | Daily CLI calls against the running `127.0.0.1:1933` test server |
| Dev | `/Users/rocke_dong/codes/ov1/.venv/bin/ov` | Active repo development and source-level CLI checks |
| Prod-sim CLI | `/opt/homebrew/bin/ov` | Optional npm CLI install behavior; invoke explicitly |

`/opt/homebrew/bin/ov` is a good prod-sim CLI boundary because it is installed
outside editable checkouts and behaves like a user-facing optional CLI package
install. It should not be the default `ov` while most work targets the local
test server: that would make routine debugging depend on an older installed CLI
and hide which checkout you are exercising.

Official install distinction:

| Surface | Official install shape | Local role |
|---|---|---|
| OpenViking package / server | `pip install openviking` or source `uv sync` / editable install | Test server runs from `/Users/rocke_dong/codes/OpenViking/.venv/bin/openviking-server` |
| `ov` CLI | `npm install -g @openviking/cli` (optional CLI install) | `/opt/homebrew/bin/ov`, prod-sim CLI |

Do not treat `/opt/homebrew/bin/ov` as the production server environment. It is
only the installed CLI surface. The running server environment is whichever
`openviking-server` process owns `127.0.0.1:1933`.

Probe the active mapping before debugging CLI behavior:

```bash
type -a ov
ov --version
/Users/rocke_dong/codes/ov1/.venv/bin/ov --version
/opt/homebrew/bin/ov --version
curl -sS http://127.0.0.1:1933/health
```

## 自学循环 — Daily Edit / Restart / Verify

Use this when the binary is healthy and you just want to pick up a source
change. `kickstart -k` kills the running process and starts a fresh one
**once**; `KeepAlive=true` is not a factor here because the new process
succeeds.

```bash
cd ~/codes/OpenViking

# 1. edit any source file under openviking/, e.g.
#    $EDITOR openviking/server/app.py

# 2. restart the server — picks up the new code via editable install
launchctl kickstart -k gui/$(id -u)/com.openviking.server

# 3. verify health (should print JSON with status=ok)
curl -sS http://127.0.0.1:1933/health

# 4. tail logs to see your change in action
tail -f ~/.openviking/data/log/openviking.err.log
```

Health check expected output:

```json
{"status":"ok","healthy":true,"version":"0.4.5.dev33","auth_mode":"dev"}
```

If the version string matches `git describe --tags` of your current HEAD, the
editable install is wired correctly.

## Maintenance Window — Switching Tags

Use this when the binary might be temporarily broken: `git checkout` to an
older tag, `uv sync` to reconcile deps, or any operation where import could
fail. The point of `unload` is to take the job **out of launchd's memory** so
`KeepAlive=true` cannot trigger a crash-restart loop while you're mid-edit.

```bash
# 1. deregister from launchd — stops process AND disables KeepAlive
launchctl unload ~/Library/LaunchAgents/com.openviking.server.plist

# 2. switch code + reconcile deps
cd ~/codes/OpenViking
git checkout v0.3.20           # or any tag/branch
uv sync --extra test --extra dev

# 3. sanity-check the binary can import at all
.venv/bin/openviking-server --version

# 4. re-register with launchd — RunAtLoad fires, KeepAlive resumes
launchctl load ~/Library/LaunchAgents/com.openviking.server.plist

# 5. verify
curl -sS http://127.0.0.1:1933/health
```

Switching back to `main` is the same flow with `git checkout main` in step 2.

### Why not `kickstart -k` here?

`kickstart -k` keeps the job registered with launchd. If the binary is broken
at that moment, the kill succeeds, the new process crashes on import, and
`KeepAlive=true` sees the exit and restarts — every few seconds, indefinitely.
`unload` removes the job from launchd entirely, so `KeepAlive` has nothing to
watch. See `AGENTS.md` "Python Environment Discipline" for the full rationale.

## Recovery — When Something Breaks

| Symptom | First probe | Likely fix |
|---|---|---|
| `curl /health` fails to connect | `launchctl list \| grep openviking` | empty = job unloaded → `launchctl load` the plist |
| Job registered but `curl` fails | `tail ~/.openviking/data/log/openviking.err.log` | look for the actual Python traceback |
| Crash loop (PID changes every ~10s) | same | `launchctl unload` to stop the loop, read logs, fix, `load` |
| `EmbeddingRebuildRequiredError` | grep `ov.conf` for `allow_metadata_override` | set it to `true` (already done as of 2026-06-26) |
| `ImportError` after `git checkout` | `.venv/bin/openviking-server --version` | `uv sync --extra test --extra dev` to reconcile deps |
| Wrong binary running (e.g. langg) | `head -1 ~/.openviking/bin/start-openviking.sh` last line | re-edit `exec` line to point at repo `.venv` |

Backups of the start script live alongside it as
`start-openviking.sh.bak-before-*` — restore with `cp` if a edit goes wrong.

## Related

- `~/.openviking/ov.conf` — server config (embedding, VLM, storage).
- `~/Library/LaunchAgents/com.openviking.server.plist` — launchd manifest.
- `~/.openclaw/openclaw.json` → `plugins.entries.openviking` — oc-side plugin
  config that consumes this server.
