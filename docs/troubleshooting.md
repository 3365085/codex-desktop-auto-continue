# Troubleshooting

## Service is not running

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
journalctl --user -u codex-desktop-auto-continue.service -n 100 --no-pager
```

Re-run `./install.sh` after moving the repository or updating the installed
script.

## Another watcher already holds the lock

Only one watcher is allowed per user. Inspect the service before removing any
lock file:

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
```

Stop the existing service or process rather than starting a second copy.

## A current chat was not continued

The watcher starts at the end of existing logs. A failure recorded before the
watcher started is intentionally ignored. Continue that chat once manually;
future failures will be observed.

Avoid `--scan-existing` on the normal session root because it can revive many
old failed chats.

## Queue-only recovery

Confirm that Desktop is open and that the selected executable supports the
command:

```bash
/usr/lib/chatgpt/resources/codex queue --help
```

The installed service uses this path intentionally. It sends `continue` to the
same thread and never opens Desktop UI, so it cannot switch the conversation
you are viewing. A visible `continue` message is expected.

## No log entry after `reconnecting 1/5`

That status is an in-progress native retry, not a completed failure. The
watcher waits for a final `task_complete.error`. If Codex reconnects
successfully, no continuation is necessary and no action is logged.
