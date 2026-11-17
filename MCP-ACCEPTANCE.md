# MCP import and register — acceptance walks

Manual checks for two features on `mcp-import-and-register`:

- **Paste to rows** (phases 1–2): a config pasted into Settings → MCP servers
  becomes rows, with env, in one restart. `ef859ac`, `54912cc`.
- **The tool and its card** (phases 3–5): `mcp.register` stages the same paste
  and shows it for approval, per-server tiers included. `2df50f5`, `636c14a`,
  `c78a5c6`.

Automated coverage is `services/core/tests/test_mcp.py`, `test_brain.py`, the
desktop typecheck and `vite build`. Those never click a card: everything below
is the part that needs a person.

---

## Before you start

- Config is `~/.capri/config.yaml`. Copy it to `config.before.yaml` first — A, D,
  E and H all write to it, and every write re-dumps the whole file, so unrelated
  formatting will move. That is `save_config`, not damage.
- `npm run dev` from the repo root. Ask in the full feed unless a step says
  otherwise.
- Pasted newlines arrive whole. Both composers are textareas
  (`App.tsx`, `onChange={setDraft}`) and nothing flattens the text after that:
  `sendText` does `raw.trim()`, `_turn_send` does `text.strip()`, so only
  leading and trailing whitespace goes. What cuts a message short is *typing*
  one by hand — plain Enter sends, Shift+Enter is the newline. Two limits that
  do apply: a message over 32 000 characters is refused with `text too long`,
  and she composes the `config` argument from the conversation rather than
  forwarding the paste, so a very long config can come back mistyped.
- Two kinds of card: a plain card shows the tier chip and a `{ … }` args block.
  A server card replaces that block with one panel per server and has **no bare
  `approve`**.

The paste used by A, B, D, E and G (call it **P1**; use your own Documents path):

```json
{
  "mcpServers": {
    "fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\daeme\\Documents"],
      "env": { "DEMO_KEY": "demo-value" }
    }
  },
  "inputs": []
}
```

---

## A — approve a stdio server at tier 2

**Do** — "Register this MCP server: P1", then `approve and register`, leaving
both selects at tier 2.

**Card** — `mcp.register`, the chip `act.benign`, the countdown. Header line
"1 server / written to the config and started on approval". One `fs` panel: a
tier select reading `tier 2 — asks first`, the full command line, and a
highlighted `DEMO_KEY=demo-value` block. Buttons `deny` and
`approve and register`.

**config.yaml** — a new block:

```yaml
mcp:
  servers:
    fs:
      command: npx
      args: [-y, "@modelcontextprotocol/server-filesystem", C:\Users\daeme\Documents]
      env:
        DEMO_KEY: demo-value
      cwd: ''
      enabled: true
      tier: 2
      timeout_s: 30.0
      max_calls_per_turn: 4
```

**Her reply** — `fs (tier 2): N tools — …`. Then ask "use the fs server to list
my Documents": that must produce a **plain** tier-2 card naming
`mcp.fs.<tool>`, which is the per-call ask working on a registered server.

---

## B — deny

**Do** — the same paste on a fresh name (`fsb`), then `deny`.

**Card** — identical to A.

**config.yaml** — unchanged: no `mcp.servers.fsb`, and the file byte-identical to
before the paste.

**Her reply** — "nothing was registered: the person did not approve it. Do not
ask again unless they say so." No second attempt follows.

---

## C — a url server and a stdio one in one paste

**Do** — paste
`{"mcpServers":{"fs":{"command":"npx","args":["-y","pkg"]},"remote":{"url":"https://example.com/mcp"}}}`
and approve.

**Card** — one `fs` panel only, plus a red line: `remote — stdio servers only —
this one is a url server (https://example.com/mcp)`.

**config.yaml** — `mcp.servers.fs` present, `remote` absent.

**Her reply** — ends with `remote: not imported — stdio servers only …`.

---

## D — tier 3

**Do** — paste again as `off3`, set that server's select to
`tier 3 — never runs`, approve.

**Card** — the select is the only thing that changes.

**config.yaml** — `mcp.servers.off3` written with `tier: 3` and
**`enabled: false`** — tier 3 is saved switched off, because a process that has
already started cannot be un-started by a policy.

**Her reply** — `off3 (tier 3): saved, switched off, so nothing runs until its
tier is lowered`. The settings row shows the `off` chip, and asking her to use
one of its tools says no such tool.

**Prove no process started** — `tasklist | findstr npx` proves nothing here:
`npx` is a shim that exits once the real `node.exe` takes over, so it is absent
whether or not the server ran, and `node` matches a dozen unrelated processes.
Match the package name in the command line instead, which catches the wrapper
and the child both:

```powershell
# run before approving and again after: both must be empty
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*server-filesystem*' } |
  Select-Object ProcessId, Name, CommandLine
```

The daemon's own record is the second proof: `~/.capri/logs/core.log` (or its
`.1` backup, if it has rotated) gains `mcp server fs exposes N tool(s)` in A and
never gains a `mcp server off3 exposes …` line. Nothing but an enabled server is
started — `McpService.start` filters the rest out — so a tier-3 row cannot spawn
anything at all.

---

## E — a command that does not exist

**Do** — paste with `"command": "npxx"`, approve at tier 2.

**Card** — identical to A. Showing the bad command before it runs is the point.

**config.yaml** — the row is written anyway (`enabled: true`, `tier: 2`).

**Her reply** — `… (tier 2): saved but did not start — … could not start 'npxx':
[WinError 2] …`, reported as a failed tool. Nothing silent, no crash.

---

## F — let the card time out

**Do** — set `agent.confirm_timeout_s: 10` in `config.yaml` (10 is the minimum),
restart, paste, then do nothing.

**Card** — counts down from 10 and clears when the turn ends. No approve is ever
sent.

**config.yaml** — unchanged; no row for that server.

**Her reply** — the same denial line as B. Silence is never an approval.

---

## G — the same card in bubble mode

**Do** — collapse to the bubble (or open with `?ui=bubble`) and ask for the
paste there.

**Card** — `mcp.register`, the reason line, one names line (`fs`, plus
`— unsupported: remote` when C's paste is used), then `review`, a tier select,
`deny` and `approve tier 2`. There is no bare `approve`, and saying or typing
"yes" must not approve it — the card stays open, because a yes/no cannot pick a
tier.

---

## H — the panel import, still working after phases 3–5

**Do** — Settings → MCP servers → the "Import a config" box → paste P1 with a
different name (`fsh`, env included) → `import`.

**Card** — none. This path never asks.

**Panel** — a green `fsh imported`, the row appears, and the note reads
`imported 1 server — restarted with it`. All configured servers restart in one
pass, not one per row.

**config.yaml** — `mcp.servers.fsh.env.DEMO_KEY: demo-value` survives.

---

## I — an ordinary tier-2 card is untouched

**Do** — "write `hello` to `C:\Users\daeme\Documents\capri-tier2-check.txt`": a
path outside the open project but inside the allowed Documents root.

**Card** — a plain card, chip `act.destructive`, a `{ path, content }` JSON
block, `deny` and `approve`. No MCP panel, no tier select.

**config.yaml** — no `mcp` changes at all.

---

## What to report back, per letter

1. The card text — a screenshot is fine.
2. The `mcp.servers.<name>` block from `config.yaml`, or "absent".
3. Her exact reply.

If a card reads `no window is connected to show the approval card` instead of
showing a panel, that is the fail-closed branch: `ctx.choose` never reached the
tool. Note which letter, since that changes the diagnosis.

---

## Known gaps, so they are not hunted for

- `mcp.status()` still does not return `env`, so an imported server's
  environment is visible on the card and in `config.yaml`, not in the settings
  row.
- `mcp.save` takes no `env` parameter, so env arrives by paste only — it cannot
  be edited on an existing row.
