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
lasts 90 days. There is no local Python process and we do not use an
API key.

URL: `https://app.memvara.dev/mcp`

Claude Code: [memvara/claude-memvara](https://github.com/memvara/claude-memvara).
A loop you wrote is `pip install memvara`.

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
