# Effect and limitations

## Same-thread continuation

Codex session rollouts contain a stable thread identifier in their metadata.
When a turn ends with a recognized transient error, this project calls:

The installed service uses a hybrid recovery policy:

For an active or blocked `/goal`:

```text
Desktop window -> Goal bar "Resume Goal"
```

For an ordinary failed turn:

```text
codex queue --thread <thread-id> --message continue
```

The queue message belongs to the existing conversation. The service does not
open Desktop UI for ordinary failures, so it cannot select another ordinary
conversation. Active Goals are the intentional exception: their official Goal
button is used to preserve Goal runtime semantics. The full live Desktop-button
behavior remains available for manual runs without `--goal-desktop-ui`.

## Preserved state

- The existing transcript remains attached to the thread.
- The recorded working directory and worktree association remain unchanged.
- File changes already written to disk remain in the working tree.
- Completed tools and messages already recorded in the rollout remain visible
  to the resumed thread.

An external command that was still running when a transport failed may have
side effects even if its completion event was not recorded. A continuation
should inspect the current working tree and process state before repeating a
non-idempotent operation.

## What the watcher does not change

- It does not modify Codex Desktop or the app-server installation, and the
  installed service does not use the Electron inspector.
- It does not change model selection or enable fallback routing.
- It does not remove Codex's internal reconnect backoff or `1/5` limit.
- It does not launch or close the Desktop application.
- It does not process old failures by default.

The watcher reacts after a turn has formally failed. This is the point at
which a same-thread continuation can be requested without killing the active
turn. Removing the delay inside an in-progress reconnect sequence requires a
change to Codex's internal transport retry implementation.

## Concurrency

Do not manually press Continue while the watcher is handling the same failure.
Both actions can request a continuation. A process lock prevents two watcher
instances from running under the same user account, but it cannot coordinate
with a manual UI action.
