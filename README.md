# Codex Desktop Auto Continue

[简体中文](README.zh-CN.md)

Automatically continue transiently failed **Codex Desktop** turns on the same
thread. The watcher runs beside the official desktop app on Linux, observes
new local session events, and uses Codex app-server's Goal continuation path
when an active `/goal` turn ends because the selected model is overloaded or
its response stream repeatedly disconnects. Ordinary threads queue `continue`.

It does **not** resend the original prompt, create a replacement chat, or route
the request to another model.

> This is an independent community project. It is not an official OpenAI
> product and is not affiliated with or endorsed by OpenAI.

## What it does

```text
Codex Desktop turn
        |
        | task_complete + transient error
        v
local JSONL session log
        |
        +-- active /goal --> app-server thread/goal/set(status=active)
        |                    (Goal continuation semantics, no user message)
        |
        +-- ordinary ------> codex queue --thread <same-id> --message continue
                             (same Desktop conversation)
```

Recognized conditions include:

- `server_overloaded`
- `Selected model is at capacity`
- `http_connection_failed`
- `response_stream_connection_failed`
- `response_stream_disconnected`
- `response_too_many_failed_attempts`
- `正在重新连接` / `reconnecting` text variants

The default maximum is 10,000 successful automatic continuations per thread.
No model fallback or model routing is added. Goal continuation does not insert
a visible `continue` user message in the chat.

## Active `/goal` behavior

The Goal bar's Continue operation resumes the same Goal when the thread is idle;
it is not a replay of the original prompt. The watcher calls
`thread/goal/set(status=active)` while preserving the existing objective, budget,
and thread context. This uses Codex's own Goal runtime rather than simulating a
pixel click or adding a visible user message.

If a transient turn failure leaves the Goal `blocked` (shown as stalled in
Desktop), the watcher reactivates it just like the Goal bar's Continue action.
If the app-server call fails, an active or transiently blocked Goal falls back to
same-thread `continue` so work can still progress; the fallback is logged.
Manually paused, completed, or usage-limited Goals are not resumed automatically.

## Requirements

- Linux with a systemd user session
- Python 3.10 or newer
- Codex Desktop / ChatGPT Desktop with the bundled `codex queue` and
  `codex app-server` commands, or a compatible `codex` executable on `PATH`

No Python packages are required.

## Install

Clone the repository and run:

```bash
./install.sh
```

The installer:

1. copies the watcher to the user data directory;
2. creates a systemd user service;
3. enables and starts the service immediately.

It does not require `sudo`, does not modify the Codex application, and does
not launch the Desktop app.

Check the service:

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
```

Follow its log:

```bash
journalctl --user -u codex-desktop-auto-continue.service -f
```

An active Goal continuation is logged like this:

```text
[codex-auto-continue] requested Goal continuation on 019... after server_overloaded (1/10000)
```

An ordinary-thread continuation is logged like this:

```text
[codex-auto-continue] queued 'continue' on 019... after server_overloaded (1/10000)
```

## Run without installing

```bash
python3 codex_desktop_auto_continue.py \
  --codex-bin /usr/lib/chatgpt/resources/codex \
  --max-attempts 10000
```

Keep that process running while using Codex Desktop.

## Important behavior

- The watcher starts at the end of existing session logs. It handles failures
  written after it starts and does not revive old failed chats by default.
- It does not interrupt an active turn on `reconnecting 1/5`. Codex's native
  reconnect sequence is allowed to finish; if the turn finally fails, an active
  Goal uses Goal continuation and an ordinary thread queues `continue`. This
  preserves the thread and completed file/tool state, but it does not remove
  the built-in reconnect delay.
- Do not click Desktop's Continue button at the same time as the watcher. Both
  actions could request a continuation.
- The service starts at user login. It waits quietly when Desktop is closed;
  it does not launch Desktop itself.

See [Effect and limitations](docs/effect-and-limitations.md) and
[Troubleshooting](docs/troubleshooting.md) for details.

## Options

```text
--session-root PATH       Session JSONL root; default: CODEX_HOME/sessions
--codex-bin PATH          Executable used for `codex queue` and app-server
--message TEXT            Continuation message; default: continue
--max-attempts N          Successful continuations per thread; default: 10000
--max-queue-attempts N    Local queue attempts per failure; default: 10000
--poll-ms N               File polling interval; default: 100 ms
--queue-retry-ms N        Delay after a local queue failure; default: 250 ms
--app-server-timeout-ms N Goal continuation timeout; default: 15000 ms
--scan-existing           Process historical errors; dangerous on old logs
--all-clients             Include sessions not created by Codex Desktop
--dry-run                 Detect and log without queueing a continuation
--once                    Run one scan and exit
--version                 Print the version
```

## Uninstall

```bash
./uninstall.sh
```

## Test

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_desktop_auto_continue.py
```

## Related official documentation

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex troubleshooting and local session logs](https://learn.chatgpt.com/docs/reference/troubleshooting)

## License

[MIT](LICENSE)
