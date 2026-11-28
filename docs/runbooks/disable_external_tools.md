# Runbook: stop external tools during an incident (task.operations.015)

Use this when a tool with an external effect (today: `github_test_create_issue`)
misbehaves, a credential may have leaked, or a destination reports abuse.

## 1. Stop now

On the host that runs the tool:

```bash
python scripts/kill_switch.py engage --reason "short incident description"
```

This writes `$VATER_KILL_SWITCH_FILE` (default `~/.vater/kill-switch`). Every
running process whose grant carries the switch refuses the tool at its next
grant check, with `NOT_AUTHORIZED`:

- before a new operation is reserved;
- inside the ledger, immediately before dispatch (after approval);
- before any resend after a rate-limit wait.

A request already on the wire cannot be recalled; its outcome is settled in
step 3. For the CI evidence workflow, set the repository variable
`VATER_EXTERNAL_TOOLS_DISABLED` to `true` (Settings → Secrets and variables →
Actions → Variables): the next run engages the switch and stops before its
first request. Also remove the `run-external-evidence` label from open pull
requests.

## 2. Confirm

```bash
python scripts/kill_switch.py status
```

`"engaged": true` is required. The switch is sticky inside a process, so a
process that has seen it stays stopped even if the file is removed.

## 3. Settle what was in flight

Operations that were dispatched when the switch was engaged are resolved by
lookup only, never resent. Replaying the same `request_key` returns the stored
result, or reconciles by the operation marker; an operation that cannot be
identified returns `HUMAN_RECONCILIATION_REQUIRED` and is checked by a person
against the destination (issues labelled `vater-external-evidence`).

## 4. Revoke credentials if they may be exposed

Rotate the destination token (for GitHub Actions the job token expires with the
run). Audit logs and evidence for secret values:

```bash
python scripts/audit_logs.py evidence/ docs/
```

## 5. Re-enable

Only after the cause is fixed and reviewed:

```bash
python scripts/kill_switch.py clear
```

then restart the processes (a running process that saw the switch stays
stopped), and set `VATER_EXTERNAL_TOOLS_DISABLED` back to `false`.
