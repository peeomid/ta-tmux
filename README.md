# ta: AI coding agents in plain tmux

> **This is a take on [herdr](https://herdr.dev), rebuilt on plain tmux.**
> It copies herdr's workflow (workspaces, tabs, sidebar, agent status, an agent CLI
> that agents can drive) but runs on tmux only: no extra app, no daemon, no hooks.
> It is not affiliated with herdr.

`ta` is a small command-line layer on top of [tmux](https://github.com/tmux/tmux).
It lets you start, watch, message and restore AI coding agents
(Claude Code, Codex, agy), each in its own tmux window. Nothing is written to
your agents' config.

- **Workspaces, tabs, panes** = tmux sessions, windows, panes. A sidebar and tab bar show them.
- **Agent status** (working / blocked / idle) is read from the screen text. No hooks.
- **Agents can run other agents**: `ta run`, `ta send`, `ta wait`, `ta read`.
- **Crash-safe**: after a tmux crash or reboot, `ta` rebuilds your layout and resumes each agent's conversation.
- Pure bash + Python 3 standard library. Nothing to `pip install`.

## Requirements

| Need | Version | Check |
|---|---|---|
| tmux | 3.0 or newer | `tmux -V` |
| Python 3 | 3.8 or newer | `python3 --version` |
| bash | 3.2 or newer (macOS default is fine) | |
| Agent CLIs | any of `claude`, `codex`, `agy` (all optional) | `ta doctor` |

Tested on macOS. Linux should work but is less tested.
Install tmux on macOS with `brew install tmux`.

## Install

```sh
git clone <this repo> ta-tmux
cd ta-tmux
./install.sh
```

`install.sh` does four things:

1. Links `ta` into `~/.local/bin` (change with `--bin-dir DIR`).
2. Adds one `source-file` line to `~/.tmux.conf` (a backup goes to `~/.tmux.conf.ta-backup`).
3. Copies `SKILL.md` to `~/.claude/skills/ta/` so Claude Code agents know how to use `ta` (skip with `--no-skill`).
4. Runs `ta doctor` to check your setup.

If `~/.local/bin` is not in your `PATH`, the script tells you what to add.
Remove everything with `./install.sh --uninstall`.

## First 5 minutes

```sh
ta                                  # open tmux (Ctrl-b d to leave; agents keep running)
ta run -n review codex "Review the diff in this repo"
ta ls                               # list agents and status
ta wait review                      # wait until it is done (or blocked)
ta read review 100                  # read its screen + 100 lines of history
ta send review "Also check the tests"
```

Open the key help inside tmux with `Ctrl-b ?`.

## Words used here

- **Harness**: an AI agent program (`claude`, `codex`, `agy`).
- **Target**: an agent, named by pane id (`%3`) or by name (`review`).
- **Blocked**: the agent shows a permission or question prompt and waits for a person.
- **Snapshot**: a saved copy of your layout, used to rebuild it after a crash.

## Safety (read this)

- **A blocked agent is a decision for a person.** By default `ta send`, `ta stop`,
  `ta agent prompt`, `ta agent send-keys`, `ta pane send-text` and
  `ta pane send-keys` **refuse** a target that is blocked. This stops one agent from
  approving another agent's permission prompt.
- To turn that guard off, set `TA_ALLOW_BLOCKED=1`. Only do it if you accept that
  agents can approve each other's actions. `ta doctor` warns when it is set.
- `ta stop --force` bypasses the guard for one call. Agents must never use it.
- Agents that control other agents (cross-harness orchestration) multiply risk.
  Use sandboxes, scoped credentials and least privilege.
- `ta` never reads or writes agent credentials. It reads agent session ids from
  the agents' own local session files, only to resume conversations.
- Do not send secrets in `ta send` messages. They appear on screen and in scrollback.

## Commands

### Core

| Command | What |
|---|---|
| `ta` / `ta attach [name]` | Open tmux (rebuild from snapshot after a crash). |
| `ta run [-n name] [-C dir] <harness\|cmd> [task]` | Start an agent in a new window. |
| `ta ls [--all]` | List agents: id, place, harness, status, parent. |
| `ta status <t>` | `working`, `blocked`, `idle` or `exited`. |
| `ta read <t> [N]` | Screen text + last N history lines (default 60). |
| `ta send [--wait] <t> <text>` | Type a message + Enter. Refused if blocked (see Safety). |
| `ta wait <t> [secs]` | Wait until `done`, `blocked`, `exited` or `timeout`. |
| `ta stop <t>` | Press Esc (interrupt). Refused if blocked. |
| `ta kill <t>` | Close the window. |
| `ta restore [--dry-run]` | After a crash or reboot: restart agents, resumed by session id. |
| `ta doctor` | Check tmux, Python and agent CLIs. |
| `ta fix` (`Ctrl-b F`) | Repair a broken layout (sidebar, widths). Agents are not touched. |

### Workspaces (`ta ws`)

1 workspace = 1 tmux session. Tab = tmux window. Pane = tmux pane.

| Command / key | What |
|---|---|
| `ta ws ls` | Workspaces with tabs, panes and agent status. |
| `ta ws new <name> [dir]`, `Ctrl-b W` | New workspace. |
| `Ctrl-b w`, `Ctrl-b (` / `)` | Pick a workspace; previous / next. |
| `ta ws save`, `Ctrl-b S` | Save a snapshot now (also saved every 30 s while a client is open). |
| `ta ws snapshots` | List snapshots (`~/.local/state/ta/ws/`, last 20). |
| `ta ws restore [--start] [--dry-run] [--from FILE]` | Rebuild all workspaces from the last snapshot. |
| `ta ws turn h\|v` | Put this pane and the next side by side / stacked. |
| `ta ws import-herdr <file> [--only LABEL] [--dry-run] [--start]` | Build workspaces from a herdr `workspaces.json` export. |
| `Ctrl-b a` (`ta top`) | Live agent list, refresh every 2 s. |
| `Ctrl-b e` | Show or hide the sidebar. |

The **sidebar** (left, 30 columns) lists workspaces and agents. `●` = working, `!` = waiting.
Click a line to jump there. The **tab bar** (top) shows the tabs of the current workspace.

### herdr-style commands (`ta agent|workspace|tab|pane`)

Same shape as the herdr CLI. Add `--json` for scripts. Add `--help` to any command.

| Group | Commands |
|---|---|
| `ta agent` | `list`, `get`, `read`, `send-keys`, `prompt <t> <text> [--wait]`, `wait`, `rename`, `focus`, `attach`, `start <name> --kind claude\|codex\|agy --pane %N` |
| `ta workspace` | `list`, `create`, `get`, `focus`, `rename`, `close` |
| `ta tab` | `list`, `create`, `get`, `focus`, `rename`, `close` |
| `ta pane` | `list`, `current`, `get`, `split`, `focus`, `zoom`, `rename`, `close`, `read`, `send-text`, `send-keys`, `wait-output` |

Statuses: `working`, `idle`, `done` (idle after working was seen), `blocked`, `exited`.
A bare shell (no agent) is refused by `send`, `wait`, `stop` and `status`.

## How status detection works

tmux cannot ask an agent what it is doing, so `ta` **reads the visible screen** and
matches text patterns. Blocked is checked first.

| Harness (tested version) | Working | Blocked |
|---|---|---|
| claude 2.1.x | `✶ Whirring… (4s · thinking)` | `Do you want to ...` |
| codex 0.164 | `• Working (3s • esc to interrupt)` | approval prompt |
| agy 1.4 | `⣯ Generating...` + `esc to cancel` | `requires permission ... enter Confirm` |

The status is a guess. If an agent update changes its screen text, set
`TA_WORKING_RE` / `TA_BLOCKED_RE` (extended regex) or edit the defaults at the top of `ta`.

## Session ids and restore

`ta run` writes each agent to a registry (`~/.local/state/ta/agents.tsv`).

| Harness | How the id is found | Resume command |
|---|---|---|
| claude | `ta` makes a UUID: `claude --session-id <uuid>` | `claude --resume <id>` |
| codex | Finder reads new `~/.codex/sessions/**/rollout-*.jsonl` for the same folder | `codex resume <id>` |
| agy | Finder reads new `~/.gemini/antigravity-cli/conversations/<id>.db` | `agy --conversation <id>` |

- Normal exit (`/exit`) and `ta kill` remove the agent from the registry.
- After a tmux crash or reboot, run `ta restore --dry-run`, then `ta restore`.
  Resume commands are typed, not run: press Enter in each pane (or use `--start`).
- Limits: two agy agents (or two codex agents in one folder) started within 2 s can swap ids.
  Codex may show an update prompt on resume, which `ta` reports as `blocked`.

## Configuration

| Variable | Effect |
|---|---|
| `TA_ALLOW_BLOCKED=1` | Turn the blocked-target guard off (see Safety). |
| `TA_WORKING_RE`, `TA_BLOCKED_RE` | Status patterns. |
| `TA_STATE` | State folder (default `~/.local/state/ta`). |
| `TA_SOCKET=name` | Use a separate tmux server (for tests): `TA_SOCKET=test ta ls`. |

### Claude Code sandbox

If Claude Code runs with its sandbox on, the agent needs the tmux socket. Find it with
`tmux display -p '#{socket_path}'` and add it to `sandbox.network.allowUnixSockets`
in your Claude Code settings.

## Troubleshooting

- `ta doctor` first.
- Layout or sidebar looks wrong: `ta fix` (or `Ctrl-b F`).
- Sidebar errors: `~/.local/state/ta/sidebar-error.log`.
- Status stays `idle` while the agent works: the screen text changed. Adjust `TA_WORKING_RE`.
- Try it without touching your setup: `TA_SOCKET=demo ta`, then `tmux -L demo kill-server` to remove it.

## Files

| File | What |
|---|---|
| `ta` | Main script (bash). Core commands, status patterns, `doctor`. |
| `ta_hd.py` | `ta agent / workspace / tab / pane` commands. |
| `ta_ws.py` | `ta ws` commands: workspaces, snapshots, sidebar, herdr import. |
| `tmux.conf` | tmux settings: tab bar, bell flag, window names. |
| `SKILL.md` | Instructions for AI agents on how to use `ta`. |
| `install.sh` | Installer / uninstaller. |

## License

MIT. See `LICENSE`.
