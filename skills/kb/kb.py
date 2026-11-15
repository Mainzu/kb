"""kb: a claims store for facts shared between sessions in one repo.

    python -I kb.py <command> ...      (how it works: README.md)

One sealed JSON file per fact at docs/claims/<key>.json, holding the current
value, its sources (file hashes and other facts' revs) and its history.
Status (clean / suspect / unchecked) is computed each time, never stored.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

STORE = Path("docs") / "claims"
KEY_RE = re.compile(r"[a-z0-9][a-z0-9._@-]*(/[a-z0-9][a-z0-9._@-]*)+")
# The file format: every field in this order. `check` is written only when set.
FIELDS = ["key", "rev", "value", "status", "sources", "check", "owner", "pinned",
          "note", "by", "at", "confirms", "flags", "history", "seal"]


class Refused(Exception):
    """A command that won't do what was asked; nothing was written."""


class NotFound(Exception):
    pass


class BadSeal(Exception):
    pass


# --- repo and time ---

def readonly():
    """KB_READONLY set (and not "0"): kb changes nothing on disk."""
    return os.environ.get("KB_READONLY", "") not in ("", "0")


# Optional locks off: a read mustn't make git refresh (write) its index.
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


def git(root, *args):
    # stdin=DEVNULL: on Windows a child inheriting stdin waits behind
    # hook_session's pending read when the hook's stdin stays open.
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=os.environ | GIT_ENV)
    return p.stdout.strip() if p.returncode == 0 else ""


def repo_root():
    top = git(Path.cwd(), "rev-parse", "--show-toplevel")
    if not top:
        raise Refused("not inside a git repository")
    return Path(top)


_HOOK_SESSION = [None]  # a SessionStart hook gets its session id on stdin


def session():
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or _HOOK_SESSION[0] or "unknown"


# --- the file format and seal ---

def seal_of(d):
    body = {k: v for k, v in d.items() if k != "seal"}
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def ordered(d):
    return {k: d[k] for k in FIELDS if k in d and not (k == "check" and d[k] is None)}


def sealed(d):
    """(the fact sealed, its file's bytes)."""
    d = ordered(d)
    d["seal"] = seal_of(d)
    return d, (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_fact(path, d):
    d, data = sealed(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return d


def read_fact(path, trust=True):
    """The fact at `path`. With trust, a hand-edited file (bad seal) raises."""
    if not path.exists():
        raise NotFound(f"no such fact: {path.stem}")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise BadSeal(f"{path.name}: not valid JSON ({e})")
    if trust and d.get("seal") != seal_of(d):
        raise BadSeal(f"{d.get('key', path.stem)}: bad seal (edited by hand?); re-run kb set")
    return d


# --- sources ---

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


HEADING_RE = re.compile(rb"(#{1,6})[ \t]+(.*?)[ \t\r]*")
FENCE_RE = re.compile(rb"[ \t]{0,3}(```|~~~)")


_BASH = []


def git_bash():
    """Git Bash's bash.exe, found from git's install folder (the `bash` on PATH
    may be WSL's): the first folder above `git --exec-path` with bin/bash.exe."""
    if not _BASH:
        d = git(Path.cwd(), "--exec-path")
        found = next((q / "bin" / "bash.exe" for q in Path(d).parents
                      if (q / "bin" / "bash.exe").is_file()), None) if d else None
        _BASH.append(found)
    if _BASH[0] is None:
        raise Refused("Git Bash not found from `git --exec-path`; checks can't run")
    return _BASH[0]


def norm_output(text):
    return text.replace("\r\n", "\n").rstrip()


def run_check(root, check):
    """("match" | "mismatch" | "timeout", output) of a fact's check command,
    run in Git Bash from the repo root. A non-zero exit is a mismatch."""
    t = check.get("timeout", 10)
    # Git Bash's own `timeout` kills the command's msys process group: taskkill
    # /T can't, since an msys child (sleep) loses its Windows parent link and
    # would keep the pipe open. Python's timeout is only a backstop.
    p = subprocess.Popen([str(git_bash()), "-c", 'exec timeout -k 1 "$0" bash -c "$1"',
                          f"{t:g}", check["cmd"]], cwd=root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    start = time.perf_counter()
    try:
        raw, _ = p.communicate(timeout=t + 5)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL)
        p.kill()
        return "timeout", ""
    if p.returncode in (124, 137) and time.perf_counter() - start >= t:
        return "timeout", ""
    out = norm_output(raw.decode("utf-8", "replace"))
    if p.returncode == 0 and out == norm_output(check["expect"]):
        return "match", out
    return "mismatch", out


def check_problem(outcome, out, check):
    if outcome == "timeout":
        return f"check timed out after {check.get('timeout', 10):g} s"
    return f'check gave "{out.splitlines()[0] if out else ""}"'


def section_hash(data, anchor):
    """(hash, None) of the Markdown section under heading `anchor`, or
    (None, "section missing" / "section ambiguous"). The section runs from its
    heading line to the line before the next heading of the same or higher
    level; headings inside ``` or ~~~ fences don't count."""
    lines = data.splitlines(keepends=True)
    headings, fence = [], None
    for i, line in enumerate(lines):
        body = line.rstrip(b"\r\n")
        m = FENCE_RE.match(body)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        m = HEADING_RE.fullmatch(body)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).decode("utf-8", "replace")))
    hits = [h for h in headings if h[2] == anchor]
    if not hits:
        return None, "section missing"
    if len(hits) > 1:
        return None, "section ambiguous"
    start, level, _ = hits[0]
    end = next((i for i, lv, _ in headings if i > start and lv <= level), len(lines))
    # Trailing blank lines belong to whatever comes next: appending a section
    # after a blank line mustn't change the hash of the one above it.
    return hashlib.sha256(b"".join(lines[start:end]).rstrip()).hexdigest(), None


class Store:
    """One repo's store for one kb run: git lookups and facts are read once."""

    def __init__(self, root):
        self.root = root
        self._head = None
        self._common = None
        self._facts = {}    # key -> raw dict, or the exception reading it raised
        self._status = {}   # key -> (state, reasons)
        self._ignored = {}  # repo-relative path -> bool
        self._cache = None  # absolute path -> [size, mtime_ns, sha256]
        self._cache_dirty = False
        self.check_notes = {}  # key -> why its check didn't re-confirm it, this run
        self.event = {}        # this run's fields for its events.csv row
        self.events = []       # check-run rows, written with the command's row
        self.counts = {}       # facts / suspect / unchecked, from the last render

    def head(self):
        if self._head is None:
            self._head = git(self.root, "rev-parse", "--short", "HEAD") or "none"
        return self._head

    def common_dir(self):
        """git's common dir (the main checkout's .git, shared by worktrees)."""
        if self._common is None and (self.root / ".git").is_dir():
            self._common = self.root / ".git"  # a main checkout: no git call needed
        if self._common is None:
            d = git(self.root, "rev-parse", "--git-common-dir")
            self._common = (self.root / d).resolve() if d else self.root / ".git"
        return self._common

    def main_root(self):
        return self.common_dir().parent

    # --- finding and hashing sources ---

    def locate(self, rel):
        """Where a path source's file is: here, or for an ignored file missing
        here, the same path in the main checkout. None when it's in neither."""
        p = self.root / rel
        if p.is_file():
            return p
        main = self.main_root() / rel
        if main.resolve() != p.resolve() and self.ignored(rel) and main.is_file():
            return main
        return None

    def cache_file(self):
        return self.common_dir() / "kb-hash-cache.json"

    def file_hash(self, p):
        """sha256 of a file's bytes, cached by absolute path, size and mtime."""
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_file().read_text(encoding="utf-8"))
                assert isinstance(self._cache, dict)
            except Exception:
                self._cache = {}  # missing or corrupt: rebuilt as we go
        st = p.stat()
        k = os.path.normcase(str(p.resolve()))
        hit = self._cache.get(k)
        if isinstance(hit, list) and hit[:2] == [st.st_size, st.st_mtime_ns]:
            return hit[2]
        h = sha256_file(p)
        self._cache[k] = [st.st_size, st.st_mtime_ns, h]
        self._cache_dirty = True
        return h

    def save_cache(self):
        if not self._cache_dirty or readonly():
            return
        try:
            f = self.cache_file()
            # Files that were moved or deleted don't stay in the cache.
            self._cache = {k: v for k, v in self._cache.items() if os.path.exists(k)}
            tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            os.replace(tmp, f)
            self._cache_dirty = False
        except OSError:
            pass  # a cache that can't be written only costs time

    def source_hash(self, src):
        """(hash, None) for a path source as it is now, or (None, problem):
        "missing", "ignored" (missing here and in the main checkout), or a
        section problem."""
        p = self.locate(src["path"])
        if p is None:
            return None, "ignored" if self.ignored(src["path"]) else "missing"
        if src.get("anchor"):
            return section_hash(p.read_bytes(), src["anchor"])
        return self.file_hash(p), None

    def at(self):
        """ISO time and short HEAD commit, the `at` of a write."""
        t = datetime.now().astimezone().isoformat(timespec="seconds")
        return f"{t} @ {self.head()}"

    def path(self, key):
        return self.root / STORE / (key + ".json")

    def exists(self, key):
        return bool(KEY_RE.fullmatch(key)) and self.path(key).is_file()

    def load(self, key, trust=True):
        if key not in self._facts:
            try:
                self._facts[key] = read_fact(self.path(key), trust=False)
            except (NotFound, BadSeal) as e:
                self._facts[key] = e
        d = self._facts[key]
        if isinstance(d, Exception):
            raise d
        if trust and d.get("seal") != seal_of(d):
            raise BadSeal(f"{key}: bad seal (edited by hand?); re-run kb set")
        return d

    def save(self, d):
        self._facts[d["key"]] = write_fact(self.path(d["key"]), d)
        self._status.clear()

    def keys(self):
        d = self.root / STORE
        if not d.is_dir():
            return []
        return sorted(p.relative_to(d).with_suffix("").as_posix() for p in d.rglob("*.json"))

    def ignored(self, rel):
        if rel not in self._ignored:
            p = subprocess.run(["git", "check-ignore", "-q", "--", rel], cwd=self.root,
                               capture_output=True, stdin=subprocess.DEVNULL,
                               env=os.environ | GIT_ENV)
            self._ignored[rel] = p.returncode == 0
        return self._ignored[rel]

    def depends_on(self, start, target, seen=None):
        """Does fact `start` reach `target` through its key sources?"""
        if start == target:
            return True
        seen = seen if seen is not None else set()
        if start in seen:
            return False
        seen.add(start)
        try:
            d = self.load(start, trust=False)
        except (NotFound, BadSeal):
            return False
        return any("key" in s and self.depends_on(s["key"], target, seen) for s in d["sources"])

    # --- status ---

    def status(self, key, active=None):
        """(state, reasons) of one fact: clean, suspect, unchecked or retired.
        A fact is suspect when a source file changed or went missing, a source
        fact changed, retired or is itself suspect (the forward walk), or it
        has a flag. Unchecked: an ignored source file isn't here."""
        if key in self._status:
            return self._status[key]
        active = active if active is not None else set()
        if key in active:
            return "suspect", [f"cycle through {key}"]
        active.add(key)
        try:
            res = self._status_of(self.load(key), active)
        except (NotFound, BadSeal) as e:
            res = "suspect", [str(e)]
        active.discard(key)
        if res[0] == "suspect" and key in self.check_notes:
            res = res[0], res[1] + [self.check_notes[key]]
        self._status[key] = res
        return res

    def _status_of(self, d, active):
        if d["status"] == "retired":
            return "retired", [retired_why(d)]
        suspect, unchecked = [], []
        for s in d["sources"]:
            if "path" in s:
                h, problem = self.source_hash(s)
                if problem == "ignored":
                    unchecked.append(f"not here (ignored): {s['path']}")
                elif problem == "missing":
                    suspect.append(f"missing: {s['path']}")
                elif problem:
                    suspect.append(f"{problem}: {describe_source(s)}")
                elif h != s["sha256"]:
                    suspect.append(f"changed: {describe_source(s)}")
                continue
            k = s["key"]
            try:
                src = self.load(k)
            except NotFound:
                suspect.append(f"source fact missing: {k}")
                continue
            except BadSeal:
                suspect.append(f"source fact unreadable: {k}")
                continue
            if src["status"] == "retired":
                suspect.append(f"source fact retired: {k}")
            elif src["rev"] != s["rev"]:
                suspect.append(f"source fact changed: {k}")
            else:
                state, _ = self.status(k, active)
                if state == "suspect":
                    suspect.append(f"source fact suspect: {k}")
                elif state == "unchecked":
                    unchecked.append(f"source fact unchecked: {k}")
        suspect += [f"flagged: {f['reason']}" for f in d.get("flags") or []]
        if suspect:
            return "suspect", suspect + unchecked
        if unchecked:
            return "unchecked", unchecked
        return "clean", []

    # --- check commands (run only on request: never from status()) ---

    def in_order(self, keys):
        """`keys`, each after the facts it's built on (bottom up)."""
        out, seen = [], set()

        def visit(k):
            if k in seen:
                return
            seen.add(k)
            try:
                d = self.load(k, trust=False)
            except (NotFound, BadSeal):
                d = {"sources": []}
            for s in d["sources"]:
                if "key" in s:
                    visit(s["key"])
            out.append(k)

        for k in self.keys():
            visit(k)
        want = set(keys)
        return [k for k in out if k in want]

    def sources_clean(self, d):
        return all(self.status(s["key"])[0] == "clean" for s in d["sources"] if "key" in s)

    def confirm_by_check(self, d, out):
        """Re-confirm a fact whose check matched; False if it can't be (a
        source file gone)."""
        try:
            sources = self.rehash(d["sources"])
        except Refused as e:
            self.check_notes[d["key"]] = f"check passed but can't confirm: {e}"
            self._status.clear()
            return False
        d.update(sources=sources, flags=[])
        d.setdefault("confirms", []).append(
            {"by": session(), "at": self.at(), "note": f"check passed: {out[:200]}"})
        self.save(d)
        return True

    def auto_check(self, key):
        """Run a suspect fact's check, bottom up; a match re-confirms it."""
        try:
            d = self.load(key)
        except (NotFound, BadSeal):
            return
        if d["status"] != "active" or not d.get("check") or readonly():
            return
        if self.status(key)[0] != "suspect" or not self.sources_clean(d):
            return
        outcome, out = self.run_check(key, d["check"])
        if outcome == "match":
            self.confirm_by_check(d, out)
        else:
            self.check_notes[key] = check_problem(outcome, out, d["check"])
            self._status.clear()

    def run_check(self, key, check):
        """run_check, recorded as a check-run event."""
        t = time.perf_counter()
        outcome, out = run_check(self.root, check)
        self.events.append({"command": "check-run", "keys": key, "outcome": outcome,
                            "reason": "" if outcome == "match" else check_problem(outcome, out, check),
                            "seconds": round(time.perf_counter() - t, 3)})
        return outcome, out

    # --- sources for a write ---

    def resolve_from(self, arg):
        """One --from argument as a source dict (hash/rev filled in)."""
        if arg.startswith("key:"):
            return self.key_source(arg[4:])
        if arg.startswith("./"):
            return self.path_source(arg[2:])
        path_part = arg.split("#", 1)[0]
        as_key = self.exists(arg)
        as_path = (Path.cwd() / path_part).is_file()
        if as_key and as_path:
            raise Refused(f"--from {arg} is both a fact and a file; write key:{arg} or ./{arg}")
        if as_key:
            return self.key_source(arg)
        return self.path_source(arg)

    def key_source(self, key, clean=False):
        """A key source at the fact's current rev. With clean, as a confirm
        needs it: the source fact must itself be clean (bottom up)."""
        if not self.exists(key):
            raise Refused(f"no such fact: {key}")
        d = self.load(key)
        if d["status"] == "retired":
            raise Refused(f"source fact {key} is retired")
        if clean:
            state, reasons = self.status(key)
            if state != "clean":
                raise Refused(f"source fact {key} is {state} ({'; '.join(reasons)}); "
                              "confirm it first (bottom up)")
        return {"key": key, "rev": d["rev"]}

    def path_source(self, arg):
        path, _, anchor = arg.partition("#")
        p = (Path.cwd() / path).resolve()
        try:
            rel = p.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            raise Refused(f"{path} is outside the repository")
        src = {"path": rel}
        if anchor:
            src["anchor"] = anchor
        h, problem = self.source_hash(src)
        if problem in ("missing", "ignored"):
            raise Refused(f"no such file or fact: {arg}")
        if problem:
            raise Refused(f"{problem}: {arg} (cite a heading that appears exactly once, "
                          "or a fact key)")
        src["sha256"] = h
        return src

    def rehash(self, sources):
        """The sources re-recorded as they are now, for a confirm."""
        out = []
        for s in sources:
            if "key" in s:
                out.append(self.key_source(s["key"], clean=True))
                continue
            h, problem = self.source_hash(s)
            if problem in ("missing", "ignored"):
                raise Refused(f"source file {s['path']} isn't here; "
                              "re-set the fact with its new path")
            if problem:
                raise Refused(f"{problem}: {describe_source(s)}; re-set the fact with its new heading")
            out.append(s | {"sha256": h})
        return out


def describe_source(s):
    if "key" in s:
        return f"fact {s['key']} (rev {s['rev']})"
    return s["path"] + (f"#{s['anchor']}" if s.get("anchor") else "")


def retired_why(d):
    for h in reversed(d.get("history") or []):
        if "retired" in h:
            return f"retired: {h['retired']}"
    return "retired"


def active(st, key):
    d = st.load(key)
    if d["status"] != "active":
        raise Refused(f"{key} is retired")
    return d


# --- commands ---

def cmd_set(st, a):
    key = a.key
    if not KEY_RE.fullmatch(key):
        raise Refused(f"malformed key {key!r}: lower case area/name, segments [a-z0-9][a-z0-9._@-]*")
    sources, seen = [], set()
    for s in (st.resolve_from(f) for f in a.sources):
        if (k := (s.get("path"), s.get("anchor"), s.get("key"))) not in seen:
            seen.add(k)
            sources.append(s)
    for s in sources:
        if "key" in s and st.depends_on(s["key"], key):
            raise Refused(f"source fact {s['key']} depends on {key} (a cycle)")
    if not sources and not a.owner:
        raise Refused("a fact needs --from <files or facts>, or --owner")
    check = None
    if a.check is None and (a.expect is not None or a.timeout is not None):
        raise Refused("--expect and --timeout need --check")
    if a.check is not None:
        if a.expect is None:
            raise Refused('--check needs --expect "<output>"')
        if not sources:
            raise Refused("a check needs --from <the files the command reads, or a fact> to trigger it")
        t = a.timeout if a.timeout is not None else 10
        check = {"cmd": a.check, "expect": a.expect, "timeout": int(t) if t == int(t) else t}
        outcome, out = st.run_check(key, check)
        if outcome != "match":
            raise Refused(f'{check_problem(outcome, out, check)}, expected "{a.expect}"')

    old = None
    if st.path(key).exists():
        try:
            old = st.load(key, trust=False)
        except BadSeal:
            old = None  # not even JSON: start over
    if old and old.get("status") == "retired" and not a.revive:
        raise Refused(f"{key} is retired; use --revive to set it again")

    at, by = st.at(), session()
    pinned = a.pin or (bool(old and old.get("pinned")) and not a.unpin)
    if old and old.get("value") == a.value and old.get("status") == "active":
        # The same value counts as a confirm, under confirm's rules: a note,
        # source facts clean, re-hash, clear flags, rev unchanged.
        if not a.note:
            raise Refused("same value: this is a confirm; say what you checked with --note")
        sources = [st.key_source(s["key"], clean=True) if "key" in s else s for s in sources]
        old.update(sources=sources, check=check, owner=a.owner, pinned=pinned, note=a.note,
                   flags=[])
        old.setdefault("confirms", []).append({"by": by, "at": at, "note": a.note})
        st.save(old)
        print(f"{key}: confirmed (rev {old['rev']})")
        return 0

    d = {"key": key, "rev": 1, "value": a.value, "status": "active", "sources": sources,
         "check": check, "owner": a.owner, "pinned": pinned, "note": a.note,
         "by": by, "at": at, "confirms": [], "flags": [], "history": []}
    if old:
        d["history"] = list(old.get("history") or [])
        if "value" in old and old.get("status") == "active":
            d["history"].append({k: old.get(k) for k in ("rev", "value", "sources", "owner", "by", "at")}
                                | {"until": at})
        d["rev"] = int(old.get("rev") or 0) + 1
    st.save(d)
    print(f"{key}: rev {d['rev']}")
    if len(d["value"]) > LONG:
        print(f"kb: warning: {key} is {len(d['value'])} characters, over {LONG}; split it, "
              "or move the detail to a file the fact cites", file=sys.stderr)
    return 0


def cmd_get(st, a):
    st.auto_check(a.key)
    d = st.load(a.key)
    st.event["hits"] = 1
    state, reasons = st.status(a.key)
    print(f"{d['key']} [{state}{': ' + '; '.join(reasons) if reasons else ''}] rev {d['rev']}")
    print(f"  {d['value']}")
    srcs = ", ".join(describe_source(s) for s in d["sources"]) or "none"
    print(f"  sources: {srcs}{' (owner)' if d.get('owner') else ''}")
    print(f"  by {d['by']} at {d['at']}" + (" · pinned" if d.get("pinned") else ""))
    if d.get("note"):
        print(f"  note: {d['note']}")
    if d.get("check"):
        c = d["check"]
        print(f"  check: {c['cmd']} -> {c['expect']!r} (timeout {c['timeout']:g} s)")
    if a.history:
        for c in d.get("confirms") or []:
            print(f"  confirmed by {c['by']} at {c['at']}: {c['note']}")
        for f in d.get("flags") or []:
            print(f"  flagged by {f['by']} at {f['at']}: {f['reason']}")
        for h in reversed(d.get("history") or []):
            why = f" [retired: {h['retired']}]" if "retired" in h else ""
            print(f"  rev {h['rev']} (until {h['until']}){why}: {h['value']}")
    return 0


def cmd_check(st, a):
    for key in st.in_order(st.keys()):
        st.auto_check(key)
    counts, states = {"clean": 0, "suspect": 0, "unchecked": 0}, {}
    for key in st.keys():
        try:
            if st.load(key)["status"] != "active":
                continue
        except (NotFound, BadSeal):
            pass  # status() reports it as suspect
        state, reasons = st.status(key)
        counts[state] += 1
        states[key] = state
        if state != "clean":
            print(f"{key} [{state}]: {'; '.join(reasons)}")
    track(st, states)
    st.event.update(facts=sum(counts.values()), suspect=counts["suspect"],
                    unchecked=counts["unchecked"])
    print(f"{sum(counts.values())} facts: {counts['clean']} clean, "
          f"{counts['suspect']} suspect, {counts['unchecked']} unchecked")
    return 0


def cmd_verify(st, a):
    """Run checks whether the facts are suspect or not. A match re-confirms a
    suspect fact; a mismatch adds a flag. Exit 1 if any check failed."""
    keys = a.keys or [k for k in st.keys() if has_check(st, k)]
    st.event["keys"] = ";".join(keys)
    failed = False
    for key in st.in_order(keys):
        d = active(st, key)
        if not d.get("check"):
            print(f"{key}: no check")
            continue
        suspect = st.status(key)[0] == "suspect"
        outcome, out = st.run_check(key, d["check"])
        if outcome == "match":
            done = suspect and st.sources_clean(d) and st.confirm_by_check(d, out)
            print(f"{key}: match" + (", confirmed" if done else ""))
            continue
        failed = True
        problem = check_problem(outcome, out, d["check"])
        if outcome == "mismatch":
            d.setdefault("flags", []).append({"by": session(), "at": st.at(), "reason": problem})
            st.save(d)
        print(f"{key}: {problem}")
    return 1 if failed else 0


def has_check(st, key):
    try:
        d = st.load(key)
    except (NotFound, BadSeal):
        return False
    return d["status"] == "active" and bool(d.get("check"))


def cmd_confirm(st, a):
    if not a.note:
        raise Refused('say what you checked: kb confirm <key> "<what I checked>"')
    d = active(st, a.key)
    d.update(sources=st.rehash(d["sources"]), flags=[])
    d.setdefault("confirms", []).append({"by": session(), "at": st.at(), "note": a.note})
    st.save(d)
    print(f"{a.key}: confirmed (rev {d['rev']})")
    return 0


def cmd_flag(st, a):
    if not a.reason:
        raise Refused('say why: kb flag <key> "<why>"')
    d = active(st, a.key)
    d.setdefault("flags", []).append({"by": session(), "at": st.at(), "reason": a.reason})
    st.save(d)
    print(f"{a.key}: flagged")
    return 0


def cmd_retire(st, a):
    if not a.why:
        raise Refused('say why: kb retire <key> "<why>"')
    d = active(st, a.key)
    at = st.at()
    d.setdefault("history", []).append(
        {k: d.get(k) for k in ("rev", "value", "sources", "owner", "by", "at")}
        | {"until": at, "retired": a.why})
    d["status"] = "retired"
    st.save(d)
    print(f"{a.key}: retired")
    return 0


def cmd_reseal(st, a):
    """For tools that rewrite fact files on purpose (a redacted export): new
    seals, and with --sources the current hashes of active facts' files.
    Adds no history, confirms or flags; rev, by and at stay."""
    facts, bad = {}, []
    for key in st.keys():
        name = f"{STORE.as_posix()}/{key}.json"
        try:
            d = json.loads(st.path(key).read_text(encoding="utf-8"))
        except ValueError:
            bad.append(f"{name} isn't valid JSON")
            continue
        if not isinstance(d, dict) or d.get("key") != key:
            bad.append(f"{name} holds key {d.get('key') if isinstance(d, dict) else None!r}")
            continue
        facts[key] = d
    if bad:
        raise Refused("nothing written: " + "; ".join(bad))
    written = broken = hashes = touched = 0
    kept = []
    for key, d in facts.items():
        broken += d.get("seal") != seal_of(d)
        if a.sources and d.get("status") == "active":
            new, changed = [], 0
            for s in d.get("sources") or []:
                if "path" in s:
                    h, problem = st.source_hash(s)
                    if problem:
                        kept.append(f"kept old hash: {key}: {problem}: {describe_source(s)}")
                    elif h != s.get("sha256"):
                        s, changed = s | {"sha256": h}, changed + 1
                new.append(s)
            if changed:
                d["sources"], hashes, touched = new, hashes + changed, touched + 1
        if sealed(d)[1] != st.path(key).read_bytes():
            write_fact(st.path(key), d)
            written += 1
    line = f"resealed {written} of {len(facts)} facts ({broken} had a bad seal)"
    print(line + (f"; updated {hashes} source hashes in {touched} facts" if a.sources else ""))
    for k in kept:
        print(k)
    return 0


def cmd_find(st, a):
    """Facts whose key, value or note contain every word. Runs no checks."""
    words = [w.lower() for w in a.words]
    hits = 0

    def match(*texts):
        hay = " ".join(t for t in texts if t).lower()
        return all(w in hay for w in words)

    for key in st.keys():
        try:
            d = st.load(key, trust=False)
        except (NotFound, BadSeal):
            continue
        if d["status"] == "active" and match(key, d.get("value"), d.get("note")):
            hits += 1
            print(f"{key} [{st.status(key)[0]}] {one_line(d['value'])}")
        if not a.all:
            continue
        if d["status"] == "retired" and match(key, d.get("value"), d.get("note")):
            hits += 1
            print(f"{key} [retired] {one_line(d['value'])}")
        for h in reversed(d.get("history") or []):
            if h.get("value") != d.get("value") and match(key, h.get("value")):
                hits += 1
                print(f"{key} [rev {h['rev']}, until {h['until']}] {one_line(h['value'])}")
    st.event["hits"] = hits
    if not hits:
        st.event["words"] = " ".join(a.words)
        print("no facts match")
    return 0


# --- STATUS.md ---

CAP = 9500  # characters: Claude Code passes at most 10,000 of hook output (spec, O1)
HEADER = [
    "# STATUS (kb render at {commit}; generated, don't edit)",
    "Before working a fact out from files, look it up with the kb skill, and record what you learn there. "
    "Suspect and unchecked facts aren't current until re-checked.",
    "",
]


def one_line(text):
    return " ".join(str(text).split())


def render_parts(st):
    """The STATUS pieces from read-only status (no checks run): suspect and
    unchecked lines, pinned lines, the Areas line, the active-fact count, and
    each active fact's state."""
    suspect, unchecked, pinned, areas, states = [], [], [], {}, {}
    st.long = []  # (key, characters) of active facts over LONG
    for key in st.keys():
        try:
            d = st.load(key, trust=False)
        except (NotFound, BadSeal):
            d = None
        if d is not None and d["status"] == "retired":
            continue
        area, _, name = key.rpartition("/")
        areas.setdefault(area, []).append(name)
        state, reasons = st.status(key)
        states[key] = state
        if d is not None and len(str(d.get("value") or "")) > LONG:
            st.long.append((key, len(str(d["value"]))))
        was = f" (was: {one_line(d['value'])})" if d is not None else ""
        if state == "suspect":
            suspect.append(f"- {key}: {'; '.join(reasons)}{was}")
        elif state == "unchecked":
            unchecked.append(f"- {key} [unchecked]: {'; '.join(reasons)}{was}")
        elif d.get("pinned"):
            pinned.append(f"- {key}: {one_line(d['value'])}")
    tops = {}
    for a, names in areas.items():
        tops[a.split("/")[0]] = tops.get(a.split("/")[0], 0) + len(names)
    index = [" · ".join(f"{t} {c}" for t, c in sorted(tops.items()))] if tops else []
    n = sum(tops.values())
    st.counts = {"facts": n, "suspect": len(suspect), "unchecked": len(unchecked)}
    return suspect + unchecked, pinned, index, n, states


# --- curating: when facts went suspect, and which areas were swept ---

CURATE_FILE = "kb-curate.json"  # in git's common dir, beside the hash cache
OVERDUE = timedelta(hours=24)
SUSPECT_ROOM = 2000  # characters of STATUS kept for Suspect when pins don't fit
LONG = 400  # characters: a longer value is split, or its detail moved to a cited file


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def curate_load(st):
    try:
        d = json.loads((st.common_dir() / CURATE_FILE).read_text(encoding="utf-8"))
        assert isinstance(d, dict)
    except Exception:
        d = {}  # missing or unreadable: starts empty
    since, swept = d.get("suspect_since"), d.get("swept")
    return {"suspect_since": since if isinstance(since, dict) else {},
            "swept": swept if isinstance(swept, dict) else {}}


def curate_save(st, d):
    if readonly():
        return
    try:
        f = st.common_dir() / CURATE_FILE
        tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass  # only the ages are lost


def track(st, states):
    """Note when each fact not current was first seen so; drop the rest.
    Sets st.overdue to the keys not current for over a day."""
    d = curate_load(st)
    old, now = d["suspect_since"], now_iso()
    since = {k: old.get(k, now) for k, s in states.items() if s in ("suspect", "unchecked")}
    if since != old:
        d["suspect_since"] = since
        curate_save(st, d)
    cut = datetime.now().astimezone() - OVERDUE
    st.overdue = set()
    for k, t in since.items():
        try:
            if datetime.fromisoformat(t) < cut:
                st.overdue.add(k)
        except (TypeError, ValueError):
            pass
    return d


def due_line(n_overdue, n_hidden, n_long=0):
    parts = []
    if n_overdue:
        parts.append(f"{n_overdue} not current for over a day")
    if n_hidden:
        parts.append(f"{n_hidden} pinned not shown")
    if n_long:
        parts.append(f"{n_long} over {LONG} characters")
    if not parts:
        return None
    return (f"Curating due: {', '.join(parts)}. Run the curate skill; unless the owner "
            "started this session for it, only on the facts your task touches.")


def layout(st, due, shown, n_bad, more, pins, hidden, index, n_facts):
    lines = [line.format(commit=st.head()) for line in HEADER]
    if due:
        lines.insert(len(lines) - 1, due)  # before the header's blank line
    lines += [f"## Suspect ({n_bad})"] + shown
    if more:
        lines.append(f"... and {more} more: kb check")
    lines += ["## Pinned"] + pins
    if hidden:
        lines.append(f"... and {hidden} pinned not shown: kb due")
    lines += [f"## Areas ({n_facts} facts; kb list <area> names them)"] + index
    return "\n".join(lines) + "\n"


def take(lines, room):
    """The leading lines that fit in `room` characters (each with its newline)."""
    shown, used = [], 0
    for line in lines:
        if used + len(line) + 1 > room:
            break
        shown.append(line)
        used += len(line) + 1
    return shown


def render_text(st, cap=CAP):
    """STATUS. Header, due line and Areas always show; Pinned fits within the
    cap less SUSPECT_ROOM; Suspect takes the rest. Sets st.hidden_pins."""
    bad, pinned, index, n, st.states = render_parts(st)
    track(st, st.states)
    od = len(st.overdue)
    st.hidden_pins = []
    text = layout(st, due_line(od, 0, len(st.long)), bad, len(bad), 0, pinned, 0, index, n)
    if len(text) <= cap:
        return text
    widest = layout(st, due_line(od, len(pinned), len(st.long)), [], len(bad), len(bad),
                    [], len(pinned), index, n)
    pins = take(pinned, cap - len(widest) - SUSPECT_ROOM)
    st.hidden_pins = pinned[len(pins):]
    hidden = len(st.hidden_pins)
    due = due_line(od, hidden, len(st.long))
    rest = len(layout(st, due, [], len(bad), len(bad), pins, hidden, index, n))
    shown = take(bad, cap - rest)
    more = len(bad) - len(shown)
    return layout(st, due, shown, len(bad), more, pins, hidden, index, n)


def cmd_due(st, a):
    """What the curate skill works from: overdue facts bottom up, pinned facts
    STATUS can't show, and the next area to sweep. Runs no checks."""
    if a.swept is not None:
        if readonly():
            raise Refused("KB_READONLY is set; due --swept writes")
        d = curate_load(st)
        d["swept"][a.swept.strip("/")] = now_iso()
        curate_save(st, d)
        return 0
    render_text(st)
    d = curate_load(st)
    since = d["suspect_since"]
    over = [k for k in st.in_order(st.keys()) if k in st.overdue]
    st.event["hits"] = len(over)
    print(f"## Overdue ({len(over)})")
    for k in over:
        print(f"- {k} since {str(since.get(k, ''))[:10]}: {'; '.join(st.status(k)[1])}")
    print(f"## Pinned not shown ({len(st.hidden_pins)})")
    for line in st.hidden_pins:
        key, _, value = line[2:].partition(": ")
        print(f"- {key} ({len(value)} chars)")
    tops = sorted({k.split("/")[0] for k in st.states})
    swept = d["swept"]
    print(f"## Long ({len(st.long)})")
    for key, n in st.long:
        print(f"- {key} ({n} chars)")
    print("## Last swept")
    for t in tops:
        print(f"- {t}: {swept.get(t) or 'never'}")
    print("## Next area")
    if tops:
        print(min(tops, key=lambda t: (str(swept.get(t, "")), t)))
    return 0



def cmd_list(st, a):
    """The index: active fact names by area, suspect and unchecked ones marked.
    With an area, that area and the areas under it. Runs no checks."""
    want = a.area.strip("/")
    areas = {}
    for key in st.keys():
        area, _, name = key.rpartition("/")
        if want and area != want and not area.startswith(want + "/"):
            continue
        try:
            d = st.load(key, trust=False)
        except (NotFound, BadSeal):
            d = None
        if d is not None and d["status"] == "retired":
            continue
        state = st.status(key)[0]
        areas.setdefault(area, []).append(name if state == "clean" else f"{name} [{state}]")
    hits = sum(len(names) for names in areas.values())
    st.event["hits"] = hits
    if not hits:
        st.event["words"] = want or "(all)"
        print(f"no facts in {want}" if want else "no facts")
    for area, names in sorted(areas.items()):
        print(f"- {area}: {', '.join(names)}")
    return 0


def cmd_render(st, a):
    text = render_text(st)
    st.event.update(st.counts, lines=text.count("\n"))
    sys.stdout.write(text)
    return 0


# --- session start ---

def project_top():
    """The repo holding the session's folder, found without calling git, so a
    session in a repo without a store pays only for starting Python."""
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    for d in (base, *base.parents):
        if (d / ".git").exists():
            return d
    return None


def fit(text, cap=CAP):
    """Header, Pinned and Areas alone can pass the cap only through uncommitted
    writes (the pre-commit hook refuses them). Claude Code turns hook output
    over 10,000 characters into a preview, so cut from the end instead."""
    tail = "... STATUS cut here: run kb render for the rest\n"
    keep, used = [], len(tail)
    for line in text.splitlines(keepends=True):
        if used + len(line) > cap:
            break
        keep.append(line)
        used += len(line)
    return "".join(keep) + tail


def hook_session():
    """A hook's session id comes as JSON on stdin (the hook environment has no
    CLAUDE_CODE_SESSION_ID). Wait at most half a second for it."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or sys.stdin is None or sys.stdin.isatty():
        return
    box = []
    t = threading.Thread(target=lambda: box.append(sys.stdin.read()), daemon=True)
    t.start()
    t.join(0.5)
    try:
        _HOOK_SESSION[0] = json.loads(box[0]).get("session_id") if box else None
    except Exception:
        pass


def cmd_hook_start(t0):
    """The global SessionStart hook: print STATUS.md into a new session. Silent
    where there's no store; never fails the session."""
    st, row = None, {"command": "hook-start", "outcome": "ok"}
    try:
        top = project_top()
        if top is None or not (top / STORE).is_dir():
            return 0
        hook_session()
        st = Store(top)
        try:
            text = render_text(st)
        finally:
            st.save_cache()
        text = text if len(text) <= CAP else fit(text)
        sys.stdout.write(text)
        row.update(st.counts, lines=text.count("\n"))
    except Exception as e:
        print(f"kb: STATUS.md not rendered: {one_line(e) or type(e).__name__}")
        row.update(outcome="error", reason=one_line(e) or type(e).__name__)
    if st is not None:
        record(st, [row | {"seconds": round(time.perf_counter() - t0, 3)}])
    return 0


# --- the write gate: init and the pre-commit hook ---

STATS_IGNORE = "/.claude/kb-stats/"
HOOK_MARK = "# kb pre-commit (installed by kb init)"
HOOK = """#!/bin/sh
{mark}: runs the repo's own hook
# (pre-commit.local) first, then checks the staged facts in docs/claims/.
hooks=$(dirname "$0")
if [ -f "$hooks/pre-commit.local" ]; then
  "$hooks/pre-commit.local" "$@" || exit $?
fi
exec python -I "{kb}" precommit
"""


def git_bytes(root, *args, input=None):
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, env=os.environ | GIT_ENV,
                       **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
    if p.returncode:
        raise Refused(f"git {args[0]} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def cmd_init(st, a):
    d = git(st.root, "rev-parse", "--git-path", "hooks")  # honours core.hooksPath and worktrees
    hooks = (st.root / d).resolve()
    hook, local = hooks / "pre-commit", hooks / "pre-commit.local"
    want = HOOK.format(mark=HOOK_MARK, kb=Path(__file__).resolve().as_posix())
    current = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else None
    if current is not None and HOOK_MARK not in current and local.exists():
        raise Refused(f"both {hook.name} and {local.name} exist in {hooks}; merge them by hand, then run kb init")
    keep = st.root / STORE / ".gitkeep"
    if not keep.exists():
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.touch()
        print(f"created {STORE.as_posix()}/")
    if not st.ignored(".claude/kb-stats/events.csv"):
        gi = st.root / ".gitignore"
        text = gi.read_bytes() if gi.exists() else b""
        if text and not text.endswith(b"\n"):
            text += b"\n"
        gi.write_bytes(text + STATS_IGNORE.encode() + b"\n")
        print(f"added {STATS_IGNORE} to .gitignore")
    if current == want:
        return 0
    if current is not None and HOOK_MARK not in current:
        os.replace(hook, local)
        print(f"moved the existing pre-commit hook to {local.name}; it runs first")
    hooks.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(want.encode("utf-8"))
    os.chmod(hook, 0o755)
    print(f"installed the pre-commit hook in {hooks.as_posix()}")
    return 0


def staged_blobs(root):
    """{repo path: bytes} of every resolved docs/claims/*.json in the index
    (GIT_INDEX_FILE, as git sets it for a pathspec commit, is honoured)."""
    entries = []
    for rec in git_bytes(root, "ls-files", "-s", "-z", "--", STORE.as_posix()).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        path = path.decode("utf-8")
        if meta.split()[2] == b"0" and path.endswith(".json"):
            entries.append((path, meta.split()[1]))
    if not entries:
        return {}
    data = git_bytes(root, "cat-file", "--batch", input=b"".join(s + b"\n" for _, s in entries))
    blobs, i = {}, 0
    for path, _ in entries:
        nl = data.index(b"\n", i)
        size = int(data[i:nl].split()[2])
        blobs[path] = data[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return blobs


def cmd_precommit(st, a):
    store = STORE.as_posix() + "/"
    changed = {p for p in git_bytes(st.root, "diff", "--cached", "--name-only", "-z",
                                    "--diff-filter=ACMRT", "--", store).decode("utf-8").split("\0")
               if p.endswith(".json")}
    if not changed:
        return 0
    problems = []
    for path, data in staged_blobs(st.root).items():
        key = path[len(store):-len(".json")]
        try:
            d = json.loads(data.decode("utf-8"))
            assert isinstance(d, dict)
        except (ValueError, AssertionError):
            if path in changed:
                problems.append(f"{path}: not valid JSON (conflict markers?); take one side whole or re-run kb set")
            continue
        if path in changed:
            if d.get("key") != key or not KEY_RE.fullmatch(key):
                problems.append(f"{path}: its key {d.get('key')!r} doesn't match its path")
            elif d.get("seal") != seal_of(d):
                problems.append(f"{path}: bad seal (edited by hand?); re-run kb set")
            elif d.get("status") == "active" and not d.get("sources") and not d.get("owner"):
                problems.append(f"{path}: an active fact with no sources needs --owner")
    for p in problems:
        print(f"kb: pre-commit: {p}", file=sys.stderr)
    if problems:
        st.event.update(outcome="refused", reason=problems[0])
    return 1 if problems else 0


# --- statistics: events.csv and kb stats ---

# One row per kb command, per check run and per session start, in the main
# checkout's .claude/kb-stats/events.csv (ignored by git). Change the columns
# here only, and bump the schema.
EVENT_SCHEMA = 1
EVENT_COLUMNS = ["schema", "at", "session", "worktree", "commit", "command", "keys", "outcome",
                 "reason", "hits", "seconds", "facts", "suspect", "unchecked", "lines", "words"]
PROJECTS = Path.home() / ".claude" / "projects"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stats_dir(st):
    return st.main_root() / ".claude" / "kb-stats"


def record(st, rows):
    """Append rows to events.csv. Never raises and never prints: a row that
    can't be written goes to errors.log if that can be written, else nowhere."""
    d = None
    try:
        if readonly() or not (st.root / STORE).is_dir():
            return
        d = stats_dir(st)
        here = st.root.resolve() == st.main_root().resolve()
        base = {"schema": EVENT_SCHEMA, "at": now_iso(), "session": session(),
                "worktree": "main" if here else st.root.name, "commit": st.head()}
        buf = io.StringIO()
        w = csv.DictWriter(buf, EVENT_COLUMNS, extrasaction="ignore", lineterminator="\n")
        f = d / "events.csv"
        d.mkdir(parents=True, exist_ok=True)
        if not f.exists() or f.stat().st_size == 0:
            w.writeheader()
        for r in rows:
            w.writerow(base | r)
        with open(f, "a", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
    except Exception as e:
        try:
            log = d / "errors.log"
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{now_iso()} {type(e).__name__}: {one_line(e)}\n")
            if log.stat().st_size > 200_000:  # keep the last 1,000 lines
                lines = log.read_text(encoding="utf-8").splitlines(True)
                log.write_text("".join(lines[-1000:]), encoding="utf-8")
        except Exception:
            pass


# kb run as a command (`kb ...` or `python ... kb.py ...` where a command
# starts), not a mention of it inside quoted text.
KB_CALL = r"""(?:^|[;&|(\n])\s*(?:kb|\S*python\S*(?:\s+-\S+)*\s+["']?\S*kb\.py["']?)\s+"""
LOOKUP_RE = re.compile(KB_CALL + r"(?:get|find|list)\b")
KB_RE = re.compile(KB_CALL + r"[a-z-]+\b")


def parse_time(text):
    try:
        t = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def project_slug(path):
    """Claude Code's folder name for a project: every character of its path
    that isn't a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def checkouts(st):
    roots = {st.root.resolve(), st.main_root().resolve()}
    for line in git(st.root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line[9:]).resolve())
    return sorted(roots)


def source_paths(st):
    """Lower-case repo paths that active facts cite."""
    out = set()
    for key in st.keys():
        try:
            d = st.load(key, trust=False)
        except (NotFound, BadSeal):
            continue
        if d["status"] == "active":
            out |= {s["path"].lower() for s in d["sources"] if "path" in s}
    return out


def tool_kind(block, roots, sources):
    """"lookup", "open" (a source file read, or named by a command) or None."""
    inp = block.get("input") or {}
    if block.get("name") in ("Bash", "PowerShell"):
        cmd = str(inp.get("command", ""))
        if LOOKUP_RE.search(cmd):
            return "lookup"
        if KB_RE.search(cmd):
            return None
        text = cmd.lower().replace("\\", "/")
        return "open" if any(p in text for p in sources) else None
    if block.get("name") == "Read":
        p = os.path.normcase(str(Path(str(inp.get("file_path", ""))).resolve()))
        for r in roots:
            base = os.path.normcase(str(r)) + os.sep
            if p.startswith(base):
                return "open" if p[len(base):].replace(os.sep, "/").lower() in sources else None
    return None


def scan_transcripts(files, roots, sources, since):
    """{session: {"first": time, "lookup": [times], "open": [times]}}. A fork's
    transcript (<session>/subagents/*.jsonl) counts toward its session."""
    sessions = {}
    for f in files:
        sid = f.parent.parent.name if f.parent.name == "subagents" else f.stem
        s = sessions.setdefault(sid, {"first": None, "lookup": [], "open": []})
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = parse_time(e.get("timestamp"))
                if e.get("type") != "assistant" or t is None or (since and t < since):
                    continue
                s["first"] = min(s["first"] or t, t)
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        kind = tool_kind(b, roots, sources)
                        if kind:
                            s[kind].append(t)
    return {k: v for k, v in sessions.items() if v["first"]}


def transcript_report(st, since):
    roots = checkouts(st)
    files = []
    for r in roots:
        d = PROJECTS / project_slug(r)
        files += sorted(d.glob("*.jsonl")) + sorted(d.glob("*/subagents/*.jsonl"))
    if not files:
        return [f"Transcripts: none found in {PROJECTS} for this repo."]
    sources = source_paths(st)
    rows = []
    for sid, s in sorted(scan_transcripts(files, roots, sources, since).items(),
                         key=lambda kv: kv[1]["first"]):
        first = min(s["lookup"], default=None)
        reopens = sum(1 for t in s["open"] if first is None or t < first)
        rows.append((sid, s["first"], len(s["lookup"]), reopens))
    lookups, reopens = sum(r[2] for r in rows), sum(r[3] for r in rows)
    out = [f"Transcripts: {len(rows)} sessions; lookups {lookups}, re-opens {reopens} "
           f"(source files read before any kb lookup, in {sum(1 for r in rows if r[3])} sessions)"]
    if not sources:
        out.append("  (no active fact cites a file, so there are no re-opens to count)")
    busy = [r for r in rows if r[2] or r[3]]
    for sid, first, n_look, n_open in busy[-15:]:
        out.append(f"  {sid[:8]} {first.astimezone():%Y-%m-%d %H:%M}: "
                   f"lookups {n_look}, re-opens {n_open}")
    if len(busy) > 15:
        out.append(f"  ... and {len(busy) - 15} earlier sessions with lookups or re-opens")
    return out


def median(xs):
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def tally(rows, field):
    """[(value, count)] of one column, most frequent first."""
    counts = {}
    for r in rows:
        counts[r.get(field)] = counts.get(r.get(field), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))


def events_report(st, since):
    f = stats_dir(st) / "events.csv"
    try:
        with open(f, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return ["Events: none recorded (.claude/kb-stats/events.csv is missing)."]
    if since:
        rows = [r for r in rows if (parse_time(r.get("at")) or since) >= since]
    if not rows:
        return ["Events: none in this period."]
    out = [f"Events: {len(rows)} rows; runs: "
           + ", ".join(f"{c} {n}" for c, n in tally(rows, "command"))]
    refused = [r for r in rows if r.get("outcome") == "refused"]
    if refused:
        out.append("  refused: " + "; ".join(f"{why} x{n}" for why, n in tally(refused, "reason")[:5]))
    missed = [r for r in rows if r.get("command") == "find" and r.get("words")]
    if missed:
        out.append("  missed lookups: " + ", ".join(f'"{w}" x{n}' for w, n in tally(missed, "words")[:10]))
    starts = [r for r in rows if r.get("command") == "hook-start" and r.get("outcome") == "ok"]
    secs = [x for x in (num(r.get("seconds")) for r in starts) if x is not None]
    if secs:
        a, b = starts[0], starts[-1]
        out.append(f"  session start: {len(starts)} runs, median {median(secs):g} s, "
                   f"slowest {max(secs):g} s; suspect {a.get('suspect')} -> {b.get('suspect')}, "
                   f"unchecked {a.get('unchecked')} -> {b.get('unchecked')}")
    runs = [r for r in rows if r.get("command") == "check-run"]
    if runs:
        slow = max((x for x in (num(r.get("seconds")) for r in runs) if x is not None), default=0)
        out.append("  checks: " + ", ".join(f"{o} {n}" for o, n in tally(runs, "outcome"))
                   + f"; slowest {slow:g} s")
    return out


def cmd_stats(st, a):
    since = None
    if a.since:
        try:
            since = datetime.fromisoformat(a.since).astimezone()
        except ValueError:
            raise Refused(f"--since {a.since}: use a date like 2026-11-02")
    print(f"kb stats{' since ' + a.since if a.since else ''}")
    print("\n".join(transcript_report(st, since) + events_report(st, since)))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="kb", description="Claims store: shared facts for this repo.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("set", help="record a fact, or a new value for one")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--from", dest="sources", nargs="+", action="extend", default=[],
                   metavar="PATH_OR_KEY")
    s.add_argument("--owner", action="store_true", help="the owner is the source")
    s.add_argument("--pin", action="store_true", help="show in full in STATUS.md")
    s.add_argument("--unpin", action="store_true")
    s.add_argument("--note")
    s.add_argument("--revive", action="store_true", help="set a retired fact again")
    s.add_argument("--check", metavar="CMD", help="a command whose output re-confirms the fact")
    s.add_argument("--expect", metavar="OUTPUT", help="the output --check must give")
    s.add_argument("--timeout", type=float, metavar="S",
                   help="seconds the check may take (default 10)")
    s.set_defaults(run=cmd_set)

    g = sub.add_parser("get", help="show one fact")
    g.add_argument("key")
    g.add_argument("--history", action="store_true")
    g.set_defaults(run=cmd_get)

    c = sub.add_parser("check", help="list every fact that isn't clean")
    c.set_defaults(run=cmd_check)

    v = sub.add_parser("verify", help="run facts' checks, suspect or not")
    v.add_argument("keys", nargs="*", metavar="key")
    v.set_defaults(run=cmd_verify)

    c = sub.add_parser("confirm", help="you checked a fact against its sources")
    c.add_argument("key")
    c.add_argument("note", nargs="?", help="what you checked (required)")
    c.set_defaults(run=cmd_confirm)

    f = sub.add_parser("flag", help="mark a fact suspect")
    f.add_argument("key")
    f.add_argument("reason", nargs="?", help="why (required)")
    f.set_defaults(run=cmd_flag)

    r = sub.add_parser("retire", help="a fact no longer holds and nothing replaces it")
    r.add_argument("key")
    r.add_argument("why", nargs="?", help="why (required)")
    r.set_defaults(run=cmd_retire)

    f = sub.add_parser("find", help="facts whose key, value or note contain every word")
    f.add_argument("words", nargs="+", metavar="word")
    f.add_argument("--all", action="store_true", help="also history and retired facts")
    f.set_defaults(run=cmd_find)

    li = sub.add_parser("list", help="the index: active fact names by area, all or one area")
    li.add_argument("area", nargs="?", default="")
    li.set_defaults(run=cmd_list)

    du = sub.add_parser("due", help="what's due for curating: overdue facts, pins not shown, next area")
    du.add_argument("--swept", metavar="AREA", help="record that AREA was just swept")
    du.set_defaults(run=cmd_due)

    rs = sub.add_parser("reseal", help="re-seal fact files a tool rewrote on purpose (not for sessions)")
    rs.add_argument("--sources", action="store_true",
                    help="also record active facts' file sources as they are now")
    rs.set_defaults(run=cmd_reseal)

    r = sub.add_parser("render", help="print STATUS.md")
    r.set_defaults(run=cmd_render)

    i = sub.add_parser("init", help="turn the store on in this repo (docs/claims, pre-commit hook)")
    i.set_defaults(run=cmd_init)

    c = sub.add_parser("precommit", help="check the staged facts (run by the pre-commit hook)")
    c.set_defaults(run=cmd_precommit)

    sub.add_parser("hook-start", help="print STATUS.md at session start (the SessionStart hook)")

    t = sub.add_parser("stats", help="how sessions use the store: transcripts and events.csv")
    t.add_argument("--since", metavar="DATE", help="only from this date on (YYYY-MM-DD)")
    t.set_defaults(run=cmd_stats)
    return p


WRITING = {"set", "confirm", "flag", "retire", "init", "verify", "reseal"}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # LF only, so the hook output's size is what CAP counts.
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    a = parser().parse_args(argv)
    t0 = time.perf_counter()
    if a.command == "hook-start":
        return cmd_hook_start(t0)
    st, outcome, reason = None, "error", ""
    try:
        st = Store(repo_root())
        if readonly() and a.command in WRITING:
            raise Refused(f"KB_READONLY is set; {a.command} writes")
        rc = a.run(st, a)
        outcome = "ok"
        return rc
    except Refused as e:
        outcome, reason = "refused", str(e)
        print(f"kb: refused: {e}", file=sys.stderr)
        return 1
    except (NotFound, BadSeal) as e:
        outcome, reason = ("not-found" if isinstance(e, NotFound) else "error"), str(e)
        print(f"kb: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        reason = one_line(e) or type(e).__name__
        raise
    finally:
        if st is not None:
            st.save_cache()
            keys = getattr(a, "key", None) or ";".join(getattr(a, "keys", None) or [])
            row = {"command": a.command, "keys": keys, "outcome": outcome, "reason": reason}
            if a.command in ("get", "find", "list"):
                row["hits"] = 0
            row |= st.event
            row["reason"] = one_line(row["reason"])[:300]
            row["seconds"] = round(time.perf_counter() - t0, 3)
            record(st, st.events + [row])


if __name__ == "__main__":
    sys.exit(main())
