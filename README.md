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
