# grok-memvara

Give Grok a memory it can prove — hosted MCP and the skill that says
how to use it.

```
grok plugin marketplace add memvara/grok-memvara
grok plugin install memvara@memvara/grok-memvara --trust
```

Pin the install to this marketplace. The plugin name `memvara` is also used by
`memvara/claude-memvara`; without the qualifier Grok refuses to guess.

The first connection opens a browser so you can click Allow. That grant
lasts 90 days, and the MCP server itself carries no API key.

The four commands below are the other way to a credential, and they do run
`python3` on this machine. Nothing else here does.

URL: `https://app.memvara.dev/mcp`

Claude Code: [memvara/claude-memvara](https://github.com/memvara/claude-memvara).
A loop you wrote is `pip install memvara`.

## Four commands for the credential itself

The grant at the top of this page is the host's, and it lasts 90 days. These four
commands get a key of your own instead, one the deployment reports no expiry for,
and say which credential this machine is actually using. They answer while the MCP
server is unauthenticated, which is when the question is worth asking.

| Command | What it does |
|---|---|
| `/memvara:authenticate` | Reports this machine's credential, and mints one unless that credential works |
| `/memvara:login` | Replaces this machine's credential with a freshly minted one |
| `/memvara:logout` | Deletes this machine's copy of the key |
| `/memvara:stats` | Reports what the store holds at this credential's scope |

The qualified spelling is the one to type. Grok's own `/login` and `/logout` keep
those bare names, so a plugin command of the same name is only reachable as
`/memvara:login` and `/memvara:logout`.

Typing one of them runs `python3` here, against `skills/memvara/scripts/memvara_auth.py` — inside the skill, which is where the library vendors it so every host gets the same copy.
Standard library only, no `pip install`, and nothing left running when the command
returns.

Each of them asks the deployment about the credential before it acts. `authenticate`
against one that already works prints what it is and stops, because minting a second
key leaves the first live on the deployment with nothing here pointing at it. Six
answers are told apart and no two share a sentence: authenticated, expired, revoked,
unrecognised, absent, and nothing answered at all. `/v1/health` carries no credential
and is asked first, so an outage is never reported as a login problem. That is the
failure these commands come from — this host's own OAuth minted a token that lived 59
minutes, and when it died every surface said some version of "not authenticated",
which cost an evening spent re-authenticating a credential that had worked fine.

**One file is written without asking: `~/.memvara/credentials.json`, mode `0600`.**
Nothing else, and it is replaced rather than truncated, since the key in it is one the
API returned exactly once. A key can also be sitting in an exported `MEMVARA_API_KEY`
or in an `Authorization` header in your client configuration; both are read, both are
named in the output, and neither is touched. This host's own OAuth client writes that
configuration too, so you are shown the block and asked, and nothing there changes
unless you say yes.

`logout` deletes that one file. The key it held still works until you revoke it in the
console at `https://app.memvara.dev`.

`authenticate` and `login` take an optional project:

```
/memvara:authenticate                 the project is chosen in the console at approval
/memvara:authenticate <project-id>    the dashed UUID the console shows for a project
```

The bare form needs a console change that is not deployed everywhere yet. Where it is
not, a request naming no project fails schema validation before the route sees it and
the deployment answers `422`. The command reads that status and names the
`<project-id>` form, rather than handing you the deployment's own refusal to work out.

A slug and a tenant id are refused rather than converted, before anything is sent. The
tenant id in particular is close enough to a UUID to be pasted by mistake, and a
credential minted against the wrong project is not an error anyone ever sees.

`stats` overlaps the `memory_stats` tool, and earns its place by answering when the MCP
server is not authenticated. When it is connected, ask the tool.

## License

Apache-2.0.

## Teach it your vocabulary

The built-in predicates are a personal-assistant vocabulary. A store of engineering facts
matches none of them, and an unknown predicate takes the safe default twice over:
multi-valued, so nothing supersedes it, and slow-decaying, so this morning's deploy still
ranks as fresh in two years. The first half shows up on the write receipt. The second is
silent.

Server-side configuration, so it is set where the server is launched:

```bash
MEMVARA_PREDICATES=engineering        # or: engineering,./ours.toml
```

A declaration outranks a guess, so a pack corrects a store that already classified
something wrongly rather than only shaping a fresh one.

## Coming from another memory product

```python
from memvara.compat import import_mem0, import_supermemory
```

mem0 records what changed and when, so that import rebuilds supersession. Supermemory
records current state, so its documents arrive as episodes on their original timestamps
and nothing invents a history it was never told — which means plain recall answers from
claims and looks empty until you ask for `include_episodes`. The skill says this at the
point of use.
