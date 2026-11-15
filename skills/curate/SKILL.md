---
name: curate
description: Keep a repo's kb claims store current and readable - fix facts that have been suspect for over a day, merge duplicates, split or shorten facts. Use when STATUS shows "Curating due", or when the owner asks to curate.
argument-hint: "[areas or keys]"
---

# Curate the claims store

Arguments: $ARGUMENTS

Work through kb only (the kb skill has the commands). Prefer trusted over
tidy: a fact keeps every hedge ("probably", "X said", ranges) and every
source, even if the store stays bigger. When unsure a fact still holds, flag
it; don't confirm it.

## Scope: decide this first

- **The owner typed `/curate` or asked in chat:** a full run over every
  area, or only the areas or keys the owner named.
- **A scheduled task the owner set up runs this** (its prompt says to run the
  curate skill): a full run over one area, `Next area`
  from `kb due`, unless its prompt names areas.
- **Anything else** (you saw "Curating due" while doing other work): work
  only on the facts your task touches, the ones it wrote, read or cited. Do step 1
  for those that are overdue and step 2's rules for those facts alone; no
  sweep, no `kb due --swept`, no stats. Pass them as arguments
  (`/curate <key or area>...`).

## Steps

1. `kb due`. Note the "Last swept" dates of the areas in scope (step 3
   needs the oldest). For each fact under Overdue, in the order listed
   (bottom up): read its sources and decide.
   - Still holds: `kb confirm <key> "<what you checked>"`.
   - Changed: `kb set` the new value with its sources.
   - Can't tell: `kb flag <key> "<why>"`.
   - No longer true, nothing replaces it: `kb retire <key> "<why>"`.
2. Sweep each area in scope: `kb list <area>`, then `kb get` each fact.
   - Two facts say the same thing: keep one, `kb retire` the other with
     "merged into <key>", and re-point facts that cite it.
   - One fact holds several claims: split it into keys of their own, then
     re-set or retire the original.
   - A value over 400 characters (`kb due` lists them under Long) or
     wordier than it needs to be: shorten it. Where a short summary can't
     hold it, move the detail to a file in the repo and cite that file (or
     its `#section`) from the fact.
   - A value that carries history ("was ...", "the name is historical"):
     keep only what is true now; kb keeps old values.
   - Then `kb due --swept <area>`.
3. Full runs: `kb stats --since <the oldest date noted in step 1>` (no
   `--since` if any was "never"); note refusals and missed lookups (words
   sessions searched for and didn't find).
4. Commit the claim files you changed, by explicit path.

## Never

- Never pin or unpin a fact: which facts every session must see is the
  owner's call.
- Never edit raw records (results, decisions log, research docs).
- Never make a fact sound surer than its sources.
- Never take over a fact that isn't overdue, unless the owner asked:
  its owner gets the first day.

## Report

End with: each changed key and what changed; the facts under "Pinned not
shown", asking the owner which to unpin; on full runs, the stats notes.
A scheduled run's final message doesn't reach the owner: send one push
notification (`PushNotification`, if the tool exists) with the counts and any
pins to decide.
