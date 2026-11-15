---
name: kb
description: Look up and record this repo's current facts (status, measurements, causes, decisions in force, how a subsystem works) in its claims store. Use in a repo with a kb store (docs/claims/, or the folder .kb.toml names) before working a fact out from files, and after learning or changing one.
---

# kb: the claims store

A repo with a kb store (`docs/claims/`, or the folder `.kb.toml` names) keeps
one sealed JSON file per fact. Each fact records the hashes of its sources;
when a source changes, the fact and every fact built on it turn **suspect**
until someone re-checks them. A new session gets STATUS.md (suspect facts,
pinned facts, fact counts per area) at start. A "Curating due" line there
means facts have been suspect past the overdue time (a day unless `.kb.toml`
sets `overdue_hours`) or pinned facts don't fit: the curate skill handles it.

Below, `kb` stands for `python -I "${CLAUDE_SKILL_DIR}/kb.py"`, in Bash
and PowerShell alike.

## Commands

- `kb find <word>...` / `kb get <key>`: look a fact up before deriving it.
  `--all` / `--history` include older values.
- `kb list [area]`: the index: active fact names by area, suspect ones marked.
- `kb set <key> "<value>" --from <path | path.md#Heading | key>...`: record a
  fact or a new value (the old one moves to history). `--owner` when the owner
  said it and no file backs it; `--pin` to show it in full in STATUS.md;
  `--note` for the command, commit or results section behind a measurement.
- `kb check`: every fact that isn't clean, with reasons.
- `kb confirm <key> "<what I checked>"`: you re-checked a suspect fact and
  it still holds. Confirm from the bottom up: source facts first.
- `kb flag <key> "<why>"`: the code or a fresh measurement disagrees. Follow
  the code or measurement, say so, and flag the fact.
- `kb retire <key> "<why>"`: no longer true and nothing replaces it.
- `kb set ... --check "<cmd>" --expect "<output>" [--timeout <s>]`: a command
  that re-confirms the fact on its own when a source changes. `kb verify`
  runs checks on demand.
- `kb due [--swept <area>]`: what's due for curating (overdue facts, pinned
  facts STATUS can't show, each area's last sweep, the next area to sweep).
- `kb render`: the full STATUS.md. `kb init [--store <folder>]`: turn the
  store on in a repo (`--store` records the folder in `.kb.toml`).
- `kb stats [--since <date>]`: lookups vs. source files re-read without one,
  missed lookups and session-start cost (from `.claude/kb-stats/events.csv`).
- `KB_READONLY=1` in the environment: kb changes nothing on disk (no statistics,
  no cache, no checks run); commands that write are refused. `readonly = true`
  in `.kb.toml` does the same for the whole repo.

## Keys

`area/name`, lower case, e.g. `status/release`, `context/python-version`,
`spec/login/status`, `decision/database`, `measurement/startup-ms@laptop`,
`cause/flaky-upload-test`, `how/cache`, `owner/open-question/pricing`.
One home per fact: elsewhere, write its key.

## Values

- At most 400 characters unless `.kb.toml` sets `value_limit`: one claim per
  fact. `kb set` warns above that.
  Detail that won't fit (a charter, a manual) goes in a file the fact cites.
- What is true now, with no history ("was ...", "the name is historical"):
  kb keeps old values (`kb get --history`).

## Citing sources

- Don't cite an append-only file (a decisions log, a hand-over file, a
  results doc) as a whole: every append would make the fact suspect. Cite its
  section with `#<heading>` (exact heading text), or a fact key.
- A measurement fact cites the context facts it depends on (the version
  measured, the machine, the benchmark), so replacing one marks it suspect.
- Results files ignored by git are fine as sources; in a worktree, kb finds
  them in the main checkout.
- A check that starts Python or reads big results may need `--timeout` above
  the 10 s default.
- Hand edits to the store's files are rejected at commit; always go through kb.
