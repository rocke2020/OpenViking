# How to Configure and Verify the Local OV VLM

> **TL;DR:** Change `vlm.model` in `~/.openviking/ov.conf`, validate the JSON,
> restart `com.openviking.server`, then check `/health` and `ov observer models`.

## Check the Current VLM

Compare the config file with the running service; they should report the same model and provider.

```bash
jq '.vlm | {provider, model, api_base}' ~/.openviking/ov.conf
ov observer models
```

`configured` means the running process loaded that model configuration. It does not prove that the
upstream model accepted a real request.

## Configure the Model

For a model-only change, edit only `vlm.model` and leave the endpoint and credentials unchanged.

```bash
${EDITOR:-vi} ~/.openviking/ov.conf
```

For example:

```diff
-    "model": "CURRENT_MODEL_ID",
+    "model": "NEW_MODEL_ID",
```

The configured endpoint must support the new model ID. Keep credentials in
`~/.openviking/tcai.env`; do not hard-code API keys in `ov.conf`.

## Validate the JSON

Both commands must succeed before restart: the first checks JSON syntax, and the second rejects
missing or empty VLM routing fields.

```bash
jq empty ~/.openviking/ov.conf
jq -e '
  (.vlm.provider | type == "string" and length > 0) and
  (.vlm.model | type == "string" and length > 0) and
  (.vlm.api_base | type == "string" and length > 0)
' ~/.openviking/ov.conf
```

## Restart the Service

A restart is required because OV loads `ov.conf` into a process-wide configuration singleton.

```bash
launchctl kickstart -k gui/$(id -u)/com.openviking.server
```

## Verify the Restart

Health must pass and `ov observer models` must show the new VLM model ID.

```bash
curl --fail --silent --show-error \
  --connect-timeout 3 --max-time 10 \
  http://127.0.0.1:1933/health | jq .

ov observer models
```

For an end-to-end provider check, run an existing VLM-backed workflow. With no separate
`query_planner`, a search against an existing non-empty session exercises the VLM:

```bash
ov search "What changed last time?" \
  --session-id EXISTING_SESSION_ID --limit 1 -o json
```

A successful response contains `"ok": true`. If `query_planner` is configured, this search tests
that model instead of `vlm`.

## Troubleshooting

Use the first failing probe to identify whether the problem is JSON, service startup, or the model
provider.

- Invalid JSON: fix the error reported by `jq empty` before restarting.
- Health fails: inspect `~/.openviking/data/log/openviking.err.log` for the startup error.
- Observer shows the old model: confirm you edited the config used by the running service, then
  restart again.
- Observer shows the new model but a live call fails: verify the model ID, endpoint, credentials,
  quota, and provider response.

See also [OpenViking Server Start Summary](../openviking-server.md), the
[configuration reference](../../en/guides/01-configuration.md), and the
[observer API](../../en/api/18-observer.md).
