# OpenViking Usage Notes

## TL;DR

Before updating the checkout that backs the local `ov` server, back up
`~/.openviking/data` and the active config files. After the update and restart,
verify that the server still points at the same data by checking `ov status` and
the VikingDB observer output.

## Safe Backup Before Updating

Run this before updating `/Users/rocke_dong/codes/OpenViking`:

```bash
tar -czf ~/openviking-data-backup-$(date +%Y%m%d-%H%M%S).tar.gz -C ~/.openviking data ov.conf ovcli.conf
```

This backs up the current server data directory plus the server and CLI config
files from `~/.openviking`.

## Post-Update Verification

After updating code and restarting the server, verify the server is connected
and the vector store still has the expected data:

```bash
ov status
ov observer vikingdb
```

If the vector count or collection summary is unexpectedly different, check that
`~/.openviking/ov.conf` still uses the intended `storage.workspace`.
