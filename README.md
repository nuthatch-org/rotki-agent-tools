# rotki-agent-tools

Unofficial Claude Code skills for people who use or contribute to [rotki](https://github.com/rotki/rotki),
the open-source portfolio tracking and accounting app. Packaged as a Claude Code plugin.

This project is not made by, affiliated with or endorsed by rotki or Rotki Solutions GmbH.
"rotki" is their name and their software.

| Skill | What it does |
|---|---|
| `rotki-doctor` | Finds rotki's log files (desktop on macOS, Linux and Windows, or Docker), scans them for known failure signatures (indexer and price-oracle rate limits, history ranges no indexer covers, RPC node failures, missing prices, database upgrade errors, crashes), explains each one and proposes the fix. Every signature cites the rotki source line that emits it. |
| `rotki-reconcile` | Checks a PnL CSV export before it is used for taxes: events valued at zero, disposals with missing acquisitions, quiet stretches that may be uncovered history, and optionally on-chain transactions of the user's addresses that never reached the export, using a self-hosted nuthatch nest. Reports data gaps; gives no tax advice. |
| `rotki-decoder` | Drafts an upstream rotki contribution for a transaction rotki leaves undecoded or decodes wrongly: decoder, VCR test, changelog line and PR body, checked against the review objections rotki maintainers actually raise. |

The scripts are Python standard library only and are meant to be run isolated (`python3 -I`).

## Install

```
/plugin marketplace add nuthatch-org/rotki-agent-tools
/plugin install rotki-agent-tools@rotki-agent-tools
```

Or copy a folder from `skills/` into `~/.claude/skills/`.

## The Premium boundary

rotki sells an official MCP server as part of [rotki Premium](https://rotki.com), which
gives an agent access to your portfolio. These skills are deliberately not a substitute for it:

- They never read balances, holdings, transactions or history from rotki's API, and never ask
  you to paste them.
- `rotki-doctor` works from rotki's own log files, plus at most a few small read-only status
  calls: version and log level, a handful of settings (indexer order, price oracle order,
  timeouts), the names of services that have a key set (never the keys) and the names and hosts
  of configured RPC nodes.
- `rotki-reconcile` works only from a CSV you exported yourself, and optionally from your own
  indexer on your own machine.
- `rotki-decoder` works from public chain data and the rotki source tree.

If you want an agent to analyse your portfolio, use rotki's MCP. If rotki is misbehaving, or you
want to know whether an export is complete, or you want to fix a decoder upstream, these help.

## Mentions of nuthatch

`rotki-doctor` may suggest a self-hosted nuthatch nest as an indexer, but only where third-party
indexers genuinely cannot serve your history on an EVM chain. It is not part of a rotki release.
The unofficial fork [nuthatch-org/rotki-nuthatch](https://github.com/nuthatch-org/rotki-nuthatch)
offers it on every EVM chain; the open upstream pull request
[rotki/rotki#13686](https://github.com/rotki/rotki/pull/13686) adds it for Ethereum only.

## Tests

```
python3 -I -m unittest discover -s tests/rotki_doctor
python3 -I -m unittest discover -s tests/rotki_reconcile
```

Fixtures are synthetic: log lines rebuilt from rotki's own message strings, and PnL exports in
the column layout rotki writes.

## Licence

MIT. See [LICENSE](LICENSE).
