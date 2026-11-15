# kb: facts that know when they're stale

A [Claude Code](https://code.claude.com) plugin that gives a repo a store of
short facts ("claims"): the current status, a measurement, why a decision was
made, how a subsystem works. Each fact records the files it came from. When
one of those files changes, the fact is marked **suspect**, and so is every
fact built on it. A new session sees the current state of the store at start,
looks a fact up instead of working it out again from files, and can tell a
fact it can trust from one it has to re-check.

It's the store I use in my own projects, packaged so others can try it.
It's a personal project: issues are welcome, but I make no promises.

## What it looks like

```
$ kb set how/parse "parse() strips whitespace and returns an int" --from calc.py
how/parse: rev 1
$ kb find parse
how/parse [clean] parse() strips whitespace and returns an int

  ... someone edits calc.py ...

$ kb check
how/parse [suspect]: changed: calc.py
1 facts: 0 clean, 1 suspect, 0 unchecked
```

Every new session in that repo starts with this in its context:

```
# STATUS (kb render at 8edae5b; generated, don't edit)
Before working a fact out from files, look it up with the kb skill, and record what you learn there. Suspect and unchecked facts aren't current until re-checked.

## Suspect (1)
- how/parse: changed: calc.py (was: parse() strips whitespace and returns an int)
## Pinned
## Areas (1 facts; kb list <area> names them)
how 1
```

Claude is the one who runs these commands; you mostly read STATUS and decide
what to pin.

## Requirements

- **Windows**, with [Git for Windows](https://git-scm.com/download/win)
  (kb runs check commands in its Git Bash). That's the only system I've used
  it on. On macOS or Linux everything except `--check` commands may work, but
  I haven't tried; `--check` refuses there, because it looks for `bash.exe`.
- **Python 3** on PATH as `python` (I use 3.13; kb uses only the standard
  library).
- **Claude Code** with plugin support (I use 2.1.317).

## Install

In Claude Code:

```
/plugin marketplace add Mainzu/kb
/plugin install kb@mainzu
```

Then, in a session in each repo that should have a store:

```
/kb:kb init
```

`kb init` creates `docs/claims/`, adds `/.claude/kb-stats/` to `.gitignore`,
and installs a git pre-commit hook. If the repo already has a pre-commit hook,
kb renames it `pre-commit.local` and runs it first; it still blocks commits
when it fails.

Repos without `docs/claims/` are untouched: the session-start hook prints
nothing there.

## What you get

- **`/kb:kb`**, the skill Claude uses to look facts up and record them. It
  loads on its own when a session needs a fact; you rarely type it. Its
  [SKILL.md](skills/kb/SKILL.md) lists every command.
- **`/kb:curate`**, which tidies the store: re-checks facts that have been
  suspect for over a day, merges duplicates, splits facts that hold several
  claims. STATUS shows a "Curating due" line when it's time.
- **A SessionStart hook** that prints STATUS (suspect facts, pinned facts,
  fact counts per area) into each new session.
- **A pre-commit hook** (installed by `kb init`) that rejects hand edits to
  `docs/claims/`. Only kb writes a valid seal, so a fact can't quietly drift
  from its sources.

## How a fact turns suspect

- **File sources** are hashed. Any change to the bytes makes the fact
  suspect. `--from notes.md#Some heading` cites one Markdown section, so
  appending to a log doesn't mark every fact that cites it.
- **Fact sources:** a fact can cite other facts (`--from measurement/speed`).
  A suspect source makes its dependents suspect too, all the way down.
- **Flags:** `kb flag <key> "<why>"` when the code or a fresh measurement
  disagrees with the fact.
- **Getting back to clean:** `kb confirm <key> "<what I checked>"`, from the
  bottom up (source facts first). A fact can also carry a check command
  (`--check "<cmd>" --expect "<output>"`) that re-confirms it automatically
  when its sources change.
- **Files ignored by git** (results, datasets) work as sources. In a git
  worktree, kb looks for them in the main checkout. A fact whose source is
  missing everywhere is shown as *unchecked*, never as clean.

Status is computed every time, never stored. One fact is one JSON file at
`docs/claims/<area>/<name>.json`, holding its value, its sources and its
history, so branches that change different facts merge without conflicts.

## Limits

- A value is meant to be one claim, at most 400 characters (`kb set` warns
  above that). Longer detail goes in a file the fact cites.
- Claude Code passes a hook at most 10,000 characters of output, so STATUS
  is kept under 9,500. The pre-commit hook refuses a commit whose pinned
  facts would push it over.
- No server, no database, no embeddings: lookups are plain word matches
  (`kb find`).

## What kb writes, and how to remove it

- `docs/claims/`: the facts. Commit them.
- `.claude/kb-stats/events.csv` in the main checkout: a local log of kb
  commands, which `kb stats` reads. Ignored by git. `kb stats` also reads
  this repo's Claude Code transcripts under `~/.claude/projects/` to count
  lookups. Nothing leaves your machine.
- `kb-hash-cache.json` in the git folder: file hashes, so big sources aren't
  re-read every session. Safe to delete; it's rebuilt.
- `.git/hooks/pre-commit`. To turn kb off in a repo, delete that file and
  rename `pre-commit.local` back to `pre-commit` if it exists.

`KB_READONLY=1` in the environment makes kb change nothing on disk.

## After updating the plugin

Claude Code keeps each plugin version in its own folder, and the pre-commit
hook names the kb.py it was installed from. After an update, run
`/kb:kb init` again in each repo, or commits there fail once the old folder
is gone.

## Tests

```
python -m pytest -q -p no:cacheprovider skills/kb
```

## License

MIT
