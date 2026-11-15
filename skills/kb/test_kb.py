"""Tests for kb (docs/specs/claims-store.md, Verification). Each test runs in
a fresh temp git repo. Most call kb's main() in-process (process starts are
slow on Windows); tests using kb_proc run the real CLI as a subprocess."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

KB = Path(__file__).resolve().parent / "kb.py"
SESSION = "test-session"


def load_kb():
    spec = importlib.util.spec_from_file_location("kb", KB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KBMOD = load_kb()


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


@pytest.fixture(scope="session")
def template(tmp_path_factory):
    """A committed repo built once; each test gets a copy (git calls cost
    ~80 ms each here)."""
    r = tmp_path_factory.mktemp("template") / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "core.autocrlf", "false")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "f").write_text("one\n", encoding="utf-8")
    (r / "g").write_text("gee\n", encoding="utf-8")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def repo(template, tmp_path):
    r = tmp_path / "repo"
    shutil.copytree(template, r)
    return r


def kb(repo, *args, cwd=None):
    """kb's main() in-process, from `cwd` (default: the repo root)."""
    out, err = io.StringIO(), io.StringIO()
    here = os.getcwd()
    os.chdir(cwd or repo)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = KBMOD.main(list(args))
            except SystemExit as e:
                rc = e.code
    finally:
        os.chdir(here)
    return SimpleNamespace(returncode=rc, stdout=out.getvalue(), stderr=err.getvalue())


def kb_proc(repo, *args, cwd=None):
    """The real CLI, as a subprocess."""
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=SESSION)
    return subprocess.run([sys.executable, "-I", str(KB), *args], cwd=cwd or repo,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def ok(repo, *args, run=kb, **kw):
    p = run(repo, *args, **kw)
    assert p.returncode == 0, p.stderr + p.stdout
    return p.stdout


def refused(repo, *args):
    p = kb(repo, *args)
    assert p.returncode != 0, p.stdout
    assert "refused" in p.stderr, p.stderr
    return p.stderr


def fact(repo, key):
    return json.loads((repo / "docs" / "claims" / (key + ".json")).read_text(encoding="utf-8"))


def claim_files(repo):
    d = repo / "docs" / "claims"
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*.json")) if d.exists() else []


# --- step 2: store core ---

def test_supersede(repo):
    ok(repo, "set", "a/x", "v1", "--from", "f", run=kb_proc)
    ok(repo, "set", "a/x", "v2", "--from", "f", run=kb_proc)
    out = ok(repo, "get", "a/x", run=kb_proc)
    assert "v2" in out and "v1" not in out
    d = fact(repo, "a/x")
    assert d["rev"] == 2 and d["value"] == "v2" and d["status"] == "active"
    assert [h["value"] for h in d["history"]] == ["v1"]
    assert d["history"][0]["rev"] == 1 and d["history"][0]["until"]
    assert d["by"] == SESSION
    assert claim_files(repo) == ["a/x.json"]
    assert "v1" in ok(repo, "get", "a/x", "--history")


def test_file_format(repo):
    ok(repo, "set", "a/x", "v1", "--from", "f", "--note", "n1")
    raw = (repo / "docs/claims/a/x.json").read_bytes()
    assert b"\r" not in raw
    d = json.loads(raw)
    assert list(d)[:4] == ["key", "rev", "value", "status"] and list(d)[-1] == "seal"
    assert d["sources"] == [{"path": "f", "sha256": d["sources"][0]["sha256"]}]
    assert len(d["sources"][0]["sha256"]) == 64
    assert d["note"] == "n1" and d["at"].count("@") == 1


def test_same_value_is_a_confirm(repo):
    ok(repo, "set", "a/x", "v1", "--from", "f")
    (repo / "f").write_text("changed\n", encoding="utf-8")
    assert "suspect" in ok(repo, "get", "a/x")
    refused(repo, "set", "a/x", "v1", "--from", "f")  # same value needs a note
    ok(repo, "set", "a/x", "v1", "--from", "f", "--note", "re-read f")
    d = fact(repo, "a/x")
    assert d["rev"] == 1 and d["history"] == []
    assert d["confirms"][-1]["note"] == "re-read f"
    assert "clean" in ok(repo, "get", "a/x")


def test_from_repeated(repo):
    """Amendment 1: every --from list counts, in order, without repeats."""
    ok(repo, "set", "x/y", "v", "--from", "f", "--from", "g")
    assert [s["path"] for s in fact(repo, "x/y")["sources"]] == ["f", "g"]
    ok(repo, "set", "x/z", "v", "--from", "f", "g", "--from", "f")
    assert [s["path"] for s in fact(repo, "x/z")["sources"]] == ["f", "g"]


def test_same_value_note_replaces_note(repo):
    """Amendment 1: a same-value set's --note becomes the fact's note too."""
    ok(repo, "set", "x/y", "v", "--from", "f", "--note", "first")
    ok(repo, "set", "x/y", "v", "--from", "f", "--note", "second")
    d = fact(repo, "x/y")
    assert d["rev"] == 1 and d["note"] == "second"
    assert d["confirms"][-1]["note"] == "second"


def test_refusals(repo):
    refused(repo, "set", "a/x", "v")                      # no --from, no --owner
    refused(repo, "set", "a/x", "v", "--from", "nope")    # missing path
    refused(repo, "set", "a/x", "v", "--from", "key:no/such")  # unknown key
    refused(repo, "set", "Bad Key", "v", "--from", "f")   # malformed key
    refused(repo, "set", "single", "v", "--from", "f")    # needs area/name
    assert claim_files(repo) == []

    ok(repo, "set", "a/owner", "v", "--owner")            # owner, no sources
    assert fact(repo, "a/owner")["owner"] is True and fact(repo, "a/owner")["sources"] == []

    # cycle: a <- b <- a
    ok(repo, "set", "c/a", "va", "--from", "f")
    ok(repo, "set", "c/b", "vb", "--from", "c/a")
    refused(repo, "set", "c/a", "va2", "--from", "c/b")
    refused(repo, "set", "c/a", "va2", "--from", "c/a")   # self
    assert fact(repo, "c/a")["value"] == "va"

    # retired source and retired key
    ok(repo, "retire", "c/a", "no longer true")
    refused(repo, "set", "c/z", "vz", "--from", "c/a")
    refused(repo, "set", "c/a", "again", "--from", "f")
    ok(repo, "set", "c/a", "again", "--from", "f", "--revive")
    d = fact(repo, "c/a")
    assert d["status"] == "active" and d["value"] == "again" and d["rev"] == 2
    assert claim_files(repo) == ["a/owner.json", "c/a.json", "c/b.json"]


def test_key_source_records_rev(repo):
    ok(repo, "set", "a/x", "v1", "--from", "f")
    ok(repo, "set", "a/x", "v2", "--from", "f")
    ok(repo, "set", "a/y", "w", "--from", "a/x", "g")
    srcs = fact(repo, "a/y")["sources"]
    assert {"key": "a/x", "rev": 2} in srcs
    assert any(s.get("path") == "g" for s in srcs)


def test_ambiguous_from(repo):
    ok(repo, "set", "docs/x", "the fact", "--from", "f")
    (repo / "docs" / "x").write_text("a file\n", encoding="utf-8")
    refused(repo, "set", "a/y", "v", "--from", "docs/x")
    ok(repo, "set", "a/k", "v", "--from", "key:docs/x")
    ok(repo, "set", "a/p", "v", "--from", "./docs/x")
    assert fact(repo, "a/k")["sources"] == [{"key": "docs/x", "rev": 1}]
    assert fact(repo, "a/p")["sources"][0]["path"] == "docs/x"


def test_anchor_is_parsed(repo):
    (repo / "notes.md").write_text("# A\nx\n## B\ny\n", encoding="utf-8")
    ok(repo, "set", "a/n", "v", "--from", "notes.md#B")
    s = fact(repo, "a/n")["sources"][0]
    assert s["path"] == "notes.md" and s["anchor"] == "B"


def test_from_subfolder(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "h").write_text("h\n", encoding="utf-8")
    ok(repo, "set", "a/s", "v", "--from", "h", cwd=repo / "sub", run=kb_proc)
    assert fact(repo, "a/s")["sources"][0]["path"] == "sub/h"
    assert (repo / "docs/claims/a/s.json").exists()


def test_get_missing_and_bad_seal(repo):
    p = kb(repo, "get", "a/none")
    assert p.returncode != 0 and "no such fact" in p.stderr
    ok(repo, "set", "a/x", "v1", "--from", "f")
    path = repo / "docs/claims/a/x.json"
    path.write_text(path.read_text(encoding="utf-8").replace("v1", "hand"), encoding="utf-8")
    p = kb(repo, "get", "a/x")
    assert p.returncode != 0 and "seal" in p.stderr
    ok(repo, "set", "a/x", "v3", "--from", "f")  # set rewrites a bad-seal file
    assert fact(repo, "a/x")["value"] == "v3"
    assert "v3" in ok(repo, "get", "a/x")


# --- step 3: staleness ---

def state(repo, key):
    """The [state...] part of `kb get`'s first line."""
    line = ok(repo, "get", key).splitlines()[0]
    return line[line.index("[") + 1:line.index("]")]


def write(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")


@pytest.fixture
def chain(repo):
    """t/a from f; t/b from fact t/a; t/c from f and g."""
    ok(repo, "set", "t/a", "va", "--from", "f")
    ok(repo, "set", "t/b", "vb", "--from", "t/a")
    ok(repo, "set", "t/c", "vc", "--from", "f", "g")
    return repo


def test_suspect_forward_walk(chain):
    repo = chain
    assert ok(repo, "check").strip() == "3 facts: 3 clean, 0 suspect, 0 unchecked"
    write(repo, "f", "two\n")
    assert state(repo, "t/a") == "suspect: changed: f"
    assert state(repo, "t/b") == "suspect: source fact suspect: t/a"
    assert state(repo, "t/c") == "suspect: changed: f"
    out = kb_proc(repo, "check").stdout  # the real CLI
    assert "t/a [suspect]: changed: f" in out and "t/b [suspect]" in out and "t/c [suspect]" in out
    assert "3 facts: 0 clean, 3 suspect, 0 unchecked" in out


def test_cutoff(chain):
    repo = chain
    write(repo, "f", "two\n")
    assert "t/a is suspect" in refused(repo, "confirm", "t/b", "x")
    ok(repo, "confirm", "t/a", "re-read f")
    assert state(repo, "t/a") == "clean" and state(repo, "t/b") == "clean"
    assert fact(repo, "t/a")["rev"] == 1
    assert state(repo, "t/c") == "suspect: changed: f"
    ok(repo, "set", "t/a", "va2", "--from", "f")
    assert state(repo, "t/b") == "suspect: source fact changed: t/a"
    ok(repo, "confirm", "t/b", "still follows from va2")
    assert state(repo, "t/b") == "clean"
    assert fact(repo, "t/b")["sources"] == [{"key": "t/a", "rev": 2}]


def test_same_value_reset_follows_confirm_rules(chain):
    repo = chain
    write(repo, "f", "two\n")
    refused(repo, "set", "t/b", "vb", "--from", "t/a", "--note", "x")  # t/a suspect


def test_confirm_needs_note(chain):
    repo = chain
    refused(repo, "confirm", "t/a")
    ok(repo, "confirm", "t/a", "read f again")
    assert "confirmed by test-session" in ok(repo, "get", "t/a", "--history")
    assert "read f again" in ok(repo, "get", "t/a", "--history")


def test_missing_and_unchecked(repo):
    write(repo, ".gitignore", "results/\n")
    (repo / "results").mkdir()
    write(repo, "results/r.json", "{}\n")
    ok(repo, "set", "m/r", "result", "--from", "results/r.json")
    ok(repo, "set", "m/f", "fact", "--from", "f")
    ok(repo, "set", "m/up", "built on r", "--from", "m/r")
    (repo / "results/r.json").unlink()
    (repo / "f").unlink()
    assert state(repo, "m/r") == "unchecked: not here (ignored): results/r.json"
    assert state(repo, "m/up") == "unchecked: source fact unchecked: m/r"
    assert state(repo, "m/f") == "suspect: missing: f"
    assert "3 facts: 0 clean, 1 suspect, 2 unchecked" in ok(repo, "check")
    assert "isn't here" in refused(repo, "confirm", "m/r", "x")


def test_flag(chain):
    repo = chain
    ok(repo, "flag", "t/a", "the code says otherwise")
    assert state(repo, "t/a") == "suspect: flagged: the code says otherwise"
    assert state(repo, "t/b") == "suspect: source fact suspect: t/a"
    refused(repo, "flag", "t/a")  # needs a reason
    ok(repo, "confirm", "t/a", "checked the code")
    assert state(repo, "t/a") == "clean" and fact(repo, "t/a")["flags"] == []
    assert "flagged by" not in ok(repo, "get", "t/a", "--history")


def test_retire(chain):
    repo = chain
    refused(repo, "retire", "t/a")  # needs a why
    ok(repo, "retire", "t/a", "superseded by the gauntlet")
    assert ok(repo, "find", "va").strip() == "no facts match"
    assert "t/a [retired] va" in ok(repo, "find", "va", "--all")
    out = ok(repo, "get", "t/a", "--history")
    assert "[retired: retired: superseded by the gauntlet]" in out.splitlines()[0]
    assert state(repo, "t/b") == "suspect: source fact retired: t/a"
    refused(repo, "set", "t/a", "again", "--from", "f")
    refused(repo, "confirm", "t/a", "x")
    refused(repo, "flag", "t/a", "x")
    ok(repo, "set", "t/a", "again", "--from", "f", "--revive")
    d = fact(repo, "t/a")
    assert d["status"] == "active" and d["rev"] == 2
    assert [h["value"] for h in d["history"]] == ["va"]
    ok(repo, "retire", "t/c", "gone")
    assert "2 facts:" in ok(repo, "check")  # retired facts aren't counted


# --- step 4: source resolution ---

NOTES = """# Title
intro
## A
a1
## B
b1
```
# not a heading
```
### B sub
b2
"""


def test_anchor(repo):
    write(repo, "notes.md", NOTES)
    ok(repo, "set", "n/b", "about B", "--from", "notes.md#B")
    ok(repo, "set", "n/a", "about A", "--from", "notes.md#A")
    with open(repo / "notes.md", "a", encoding="utf-8") as f:
        f.write("## C\nc1\n")
    assert state(repo, "n/b") == "clean" and state(repo, "n/a") == "clean"
    write(repo, "notes.md", (repo / "notes.md").read_text(encoding="utf-8").replace("a1", "a2"))
    assert state(repo, "n/b") == "clean" and state(repo, "n/a") == "suspect: changed: notes.md#A"
    write(repo, "notes.md", (repo / "notes.md").read_text(encoding="utf-8").replace("b2", "b3"))
    assert state(repo, "n/b") == "suspect: changed: notes.md#B"  # a subsection is part of B
    ok(repo, "confirm", "n/b", "re-read B")
    write(repo, "notes.md", (repo / "notes.md").read_text(encoding="utf-8").replace("## B\n", "## Bee\n"))
    assert state(repo, "n/b") == "suspect: section missing: notes.md#B"
    write(repo, "notes.md", NOTES + "## B\nagain\n")
    assert state(repo, "n/b") == "suspect: section ambiguous: notes.md#B"
    assert "section ambiguous" in refused(repo, "set", "n/x", "v", "--from", "notes.md#B")
    assert "section missing" in refused(repo, "set", "n/x", "v", "--from", "notes.md#Nope")
    assert "section missing" in refused(repo, "set", "n/x", "v", "--from", "notes.md#not a heading")


def test_anchor_append_after_blank_line(repo):
    # Appending a section the usual way (a blank line, then the heading) must
    # leave the section that used to be last clean.
    write(repo, "log.md", "# Log\n\n## A\na1\n\n## B\nb1\n")
    ok(repo, "set", "n/b", "about B", "--from", "log.md#B")
    with open(repo / "log.md", "a", encoding="utf-8") as f:
        f.write("\n## C\nc1\n")
    assert state(repo, "n/b") == "clean"


def test_anchor_crlf_and_trailing_space(repo):
    (repo / "w.md").write_bytes(b"# A  \r\nx\r\n# B\r\ny\r\n")
    ok(repo, "set", "n/w", "v", "--from", "w.md#A")
    with open(repo / "w.md", "ab") as f:
        f.write(b"z\r\n")  # lands in B
    assert state(repo, "n/w") == "clean"


def test_worktree_ignored(repo, tmp_path):
    write(repo, ".gitignore", "results/\n")
    (repo / "results").mkdir()
    write(repo, "results/r.json", '{"x": 1}\n')
    ok(repo, "set", "m/r", "x is 1", "--from", "results/r.json")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fact")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "wt")
    assert not (wt / "results").exists()
    assert "1 facts: 1 clean" in ok(wt, "check")
    write(repo, "results/r.json", '{"x": 2}\n')
    assert state(wt, "m/r") == "suspect: changed: results/r.json"
    (repo / "results/r.json").unlink()
    assert state(wt, "m/r") == "unchecked: not here (ignored): results/r.json"
    # set from a worktree may cite an ignored file that only the main checkout has
    write(repo, "results/s.json", "{}\n")
    ok(wt, "set", "m/s", "s exists", "--from", "results/s.json")
    assert state(wt, "m/s") == "clean"


def test_hash_cache(repo, monkeypatch):
    big = repo / "big.bin"
    size = 200 * 1024 * 1024
    with open(big, "wb") as f:
        chunk = os.urandom(1 << 20)
        for _ in range(size >> 20):
            f.write(chunk)
    write(repo, ".gitignore", "big.bin\n")
    ok(repo, "set", "h/big", "the big file", "--from", "big.bin")
    cache = repo / ".git" / "kb-hash-cache.json"
    assert cache.exists()

    calls = []
    real = KBMOD.sha256_file
    monkeypatch.setattr(KBMOD, "sha256_file", lambda p: calls.append(p) or real(p))
    # Count hash calls, not wall time: a busy machine made a timed check flaky.
    assert "1 clean" in ok(repo, "check")
    assert calls == []  # from the cache

    st = big.stat()
    os.utime(big, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # touched, same bytes
    assert "1 clean" in ok(repo, "check") and len(calls) == 1
    assert "1 clean" in ok(repo, "check") and len(calls) == 1  # cached again

    with open(big, "r+b") as f:  # same size, one byte changed
        b = f.read(1)
        f.seek(0)
        f.write(bytes([b[0] ^ 0xFF]))
    os.utime(big, ns=(st.st_atime_ns, st.st_mtime_ns + 2 * 10**9))
    assert "1 suspect" in ok(repo, "check")

    cache.write_text("not json", encoding="utf-8")
    assert "1 suspect" in ok(repo, "check")
    assert isinstance(json.loads(cache.read_text(encoding="utf-8")), dict)
    big.unlink()  # don't leave 200 MB in pytest's kept temp folders


def test_hash_cache_drops_missing_files(repo):
    write(repo, "a.md", "a\n")
    write(repo, "b.md", "b\n")
    ok(repo, "set", "h/a", "a", "--from", "a.md")
    ok(repo, "set", "h/b", "b", "--from", "b.md")
    cache = repo / ".git" / "kb-hash-cache.json"
    keys = lambda: " ".join(json.loads(cache.read_text(encoding="utf-8")))  # noqa: E731
    assert "a.md" in keys() and "b.md" in keys()
    (repo / "a.md").unlink()
    write(repo, "b.md", "b changed\n")  # a new hash, so the cache is saved
    ok(repo, "check")
    assert "a.md" not in keys() and "b.md" in keys()


# --- step 5: check commands ---

@pytest.fixture
def checked(repo):
    """c/a from f, re-confirmed by `cat f` = 1; c/b from fact c/a."""
    (repo / "f").write_bytes(b"1\n")
    ok(repo, "set", "c/a", "f holds 1", "--from", "f", "--check", "cat f", "--expect", "1")
    ok(repo, "set", "c/b", "built on a", "--from", "c/a")
    return repo


def test_check_refusals(repo):
    (repo / "f").write_bytes(b"1\n")
    assert "check" in refused(repo, "set", "c/a", "v", "--from", "f", "--check", "cat f", "--expect", "2")
    assert "--from" in refused(repo, "set", "c/a", "v", "--owner", "--check", "cat f", "--expect", "1")
    refused(repo, "set", "c/a", "v", "--from", "f", "--check", "cat f")  # no --expect
    refused(repo, "set", "c/a", "v", "--from", "f", "--expect", "1")  # no --check
    assert claim_files(repo) == []


def test_check_stored(checked):
    assert fact(checked, "c/a")["check"] == {"cmd": "cat f", "expect": "1", "timeout": 10}


def test_check_reconfirms(checked):
    repo = checked
    os.utime(repo / "f", ns=(0, 10**18))  # touched, same bytes
    assert state(repo, "c/a") == "clean"
    (repo / "f").write_bytes(b"1\r\n")  # changed bytes, same output
    assert KBMOD.Store(repo).status("c/b")[0] == "suspect"
    out = ok(repo, "check")
    assert out.strip().endswith("2 clean, 0 suspect, 0 unchecked"), out
    d = fact(repo, "c/a")
    assert d["rev"] == 1 and d["confirms"][-1]["note"] == "check passed: 1"
    assert state(repo, "c/b") == "clean"


def test_check_mismatch(checked):
    repo = checked
    (repo / "f").write_bytes(b"2\n")
    out = ok(repo, "check")
    assert 'c/a [suspect]: changed: f; check gave "2"' in out, out
    assert "c/b [suspect]" in out


def test_get_runs_the_check(checked):
    repo = checked
    (repo / "f").write_bytes(b"1\r\n")
    assert state(repo, "c/a") == "clean"
    assert fact(repo, "c/a")["confirms"][-1]["note"] == "check passed: 1"


def test_check_bottom_up(checked):
    """c/d has its own check but sits on c/a: no check while c/a is suspect."""
    repo = checked
    (repo / "g").write_bytes(b"x\n")
    ok(repo, "set", "c/d", "d", "--from", "c/a", "g", "--check", "touch ran-d; cat g", "--expect", "x")
    (repo / "ran-d").unlink()
    (repo / "f").write_bytes(b"2\n")
    (repo / "g").write_bytes(b"x\r\n")
    assert "c/d [suspect]" in ok(repo, "check")
    assert not (repo / "ran-d").exists()


def test_check_timeout(repo, monkeypatch):
    (repo / "f").write_bytes(b"1\n")
    # set's own run of the check is stubbed: on a busy machine Git Bash alone
    # took over 1 s to start, so the fast `cat f` timed out too.
    monkeypatch.setattr(KBMOD, "run_check", lambda root, check: ("match", "1"))
    ok(repo, "set", "c/a", "v", "--from", "f", "--check",
       "if [ -e slow ]; then sleep 60; fi; cat f", "--expect", "1", "--timeout", "1")
    monkeypatch.undo()
    (repo / "slow").write_bytes(b"")
    (repo / "f").write_bytes(b"1\r\n")
    # Did Git Bash's timeout close the pipe, or did kb's backstop (its wait of
    # timeout + 5 s) give up on it? Checked, not timed: on a busy machine Git
    # Bash's whole timeout cycle took over 6 s, so here the backstop waits 35 s
    # longer, still well inside the sleep.
    backstop = []
    class Popen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            try:
                return super().communicate(input, timeout and timeout + 35)
            except subprocess.TimeoutExpired:
                backstop.append(self.args)
                raise
    monkeypatch.setattr(KBMOD.subprocess, "Popen", Popen)
    out = ok(repo, "check")
    assert backstop == [], "the sleep child kept the pipe open"
    assert "check timed out after 1 s" in out, out


def test_checks_only_on_request(repo):
    (repo / "f").write_bytes(b"1\n")
    ok(repo, "set", "c/a", "v", "--from", "f", "--check", "touch marker; cat f", "--expect", "1")
    (repo / "marker").unlink()
    (repo / "f").write_bytes(b"1\r\n")
    assert "- c/a: changed: f" in ok(repo, "render")  # render runs no checks
    assert not (repo / "marker").exists()
    out = ok(repo, "verify", "c/a")
    assert (repo / "marker").exists() and "c/a: match" in out


def test_verify(checked):
    repo = checked
    assert "c/a: match" in ok(repo, "verify")  # clean and matching: nothing written
    assert fact(repo, "c/a")["confirms"] == []
    (repo / "f").write_bytes(b"2\n")
    os.utime(repo / "f", ns=(0, 10**18))
    p = kb(repo, "verify", "c/a")
    assert p.returncode == 1 and 'c/a: check gave "2"' in p.stdout, p.stdout
    assert fact(repo, "c/a")["flags"][-1]["reason"] == 'check gave "2"'


# --- step 6: lookup and render ---

def test_find(repo):
    ok(repo, "set", "net/product", "The 10-29 Net plays as the product", "--from", "f")
    ok(repo, "set", "net/old", "burst8hp2 was the product net", "--from", "f")
    ok(repo, "set", "net/old", "burst8hp2 is retired from the ladder", "--from", "f", "--note", "ladder era")
    out = ok(repo, "find", "PRODUCT", "net")  # every word, any case, key counts
    assert out.splitlines() == ["net/product [clean] The 10-29 Net plays as the product"]
    assert "net/old" in ok(repo, "find", "ladder", "era")  # the note counts
    assert ok(repo, "find", "product", "burst8hp2").strip() == "no facts match"  # old value: history
    out = ok(repo, "find", "product", "burst8hp2", "--all")
    assert "net/old [rev 1, until" in out and "burst8hp2 was the product net" in out
    write(repo, "f", "changed\n")
    assert "net/product [suspect]" in ok(repo, "find", "10-29")


def bulk(repo, areas, per_area, value="v", prefix="area", **fields):
    """Write facts straight through kb's file writer (fast), owner-sourced so
    they're clean unless `fields` says otherwise."""
    for a in range(areas):
        for n in range(per_area):
            key = f"{prefix}-{a:02d}/fact-name-{n:03d}"
            KBMOD.write_fact(repo / "docs/claims" / (key + ".json"), {
                "key": key, "rev": 1, "value": value, "status": "active", "sources": [],
                "owner": True, "pinned": False, "note": None, "by": SESSION, "at": "t",
                "confirms": [], "flags": [], "history": []} | fields)


def test_render_layout(repo):
    write(repo, ".gitignore", "results/\n")
    (repo / "results").mkdir()
    write(repo, "results/r.json", "{}\n")
    ok(repo, "set", "p/clean", "a pinned clean fact", "--from", "g", "--pin")
    ok(repo, "set", "p/sus", "a pinned fact that went stale", "--from", "f", "--pin")
    ok(repo, "set", "p/un", "a result", "--from", "results/r.json", "--pin")
    ok(repo, "set", "q/deep/x", "not pinned", "--owner")
    ok(repo, "set", "q/gone", "retired one", "--owner")
    ok(repo, "retire", "q/gone", "gone")
    write(repo, "f", "changed\n")
    (repo / "results/r.json").unlink()
    out = ok(repo, "render", run=kb_proc)
    lines = out.splitlines()
    assert lines[0].startswith("# STATUS (kb render at ")
    assert lines[1:4] == [KBMOD.HEADER[1], "", "## Suspect (2)"] and "kb skill" in lines[1]
    assert "kb.py" not in out
    sus = out[out.index("## Suspect (2)"):out.index("## Pinned")]
    pin = out[out.index("## Pinned"):out.index("## Areas")]
    idx = out[out.index("## Areas (4 facts; kb list <area> names them)"):]
    assert "- p/sus: changed: f (was: a pinned fact that went stale)" in sus
    assert "- p/un [unchecked]: not here (ignored): results/r.json (was: a result)" in sus
    assert pin.splitlines()[1:] == ["- p/clean: a pinned clean fact"]
    assert idx.splitlines()[1:] == ["p 3 · q 1"]
    assert "gone" not in out and "q/deep" not in out


def test_list(repo, no_project_dir):
    """K-list: the index moved out of STATUS into kb list."""
    ok(repo, "set", "p/clean", "clean", "--from", "g")
    ok(repo, "set", "p/sus", "stale", "--from", "f")
    ok(repo, "set", "q/deep/x", "deep", "--owner")
    ok(repo, "set", "q/gone", "retired one", "--owner")
    ok(repo, "retire", "q/gone", "gone")
    write(repo, "f", "changed\n")
    assert ok(repo, "list").splitlines() == ["- p: clean, sus [suspect]", "- q/deep: x"]
    assert ok(repo, "list", "q/").splitlines() == ["- q/deep: x"]
    assert ok(repo, "list", "zz").strip() == "no facts in zz"
    assert "p 2 · q 1" in ok(repo, "render")
    rows = [r for r in events(repo)[1] if r["command"] == "list"]
    assert [r["hits"] for r in rows] == ["3", "1", "0"] and rows[2]["words"] == "zz"


def test_render_300_facts_fits(repo):
    bulk(repo, 12, 25)
    out = ok(repo, "render")
    assert len(out) <= KBMOD.CAP, len(out)
    assert "## Suspect (0)" in out and "## Areas (300 facts;" in out
    assert "area-00 25 · area-01 25" in out and "fact-name" not in out


def test_many_facts_commit(gated):
    """K-many: the number of facts no longer meets the cap."""
    bulk(gated, 12, 100)
    out = ok(gated, "render")
    assert len(out) < 1000, len(out)
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode == 0, p.stderr


def test_render_trims_suspects(repo):
    bulk(repo, 12, 25, value="a value of moderate length, one sentence as facts usually are",
         flags=[{"by": SESSION, "at": "t", "reason": "the code says otherwise"}])
    out = ok(repo, "render")
    assert len(out) <= KBMOD.CAP, len(out)
    shown = [l for l in out[out.index("## Suspect"):out.index("## Pinned")].splitlines()
             if l.startswith("- ")]
    assert 0 < len(shown) < 300
    assert f"... and {300 - len(shown)} more: kb check" in out
    assert "## Areas (300 facts;" in out


# --- step 7: write gate (init, precommit) ---

def commit(repo, *args, msg="c"):
    """A real `git commit` (runs the hook)."""
    return subprocess.run(["git", "commit", "-qm", msg, *args], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8")


def hooks_dir(repo):
    return repo / ".git" / "hooks"


@pytest.fixture
def gated(repo):
    """A repo with kb init run and facts a/one, a/two committed."""
    ok(repo, "init")
    ok(repo, "set", "a/one", "first", "--from", "f")
    ok(repo, "set", "a/two", "second", "--from", "g")
    git(repo, "add", "-A")
    p = commit(repo, msg="facts")
    assert p.returncode == 0, p.stderr
    return repo


def hand_edit(repo, key, value):
    p = repo / "docs/claims" / (key + ".json")
    d = json.loads(p.read_text(encoding="utf-8"))
    d["value"] = value
    p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def test_init(repo):
    ok(repo, "init")
    assert (repo / "docs/claims/.gitkeep").is_file()
    assert "/.claude/kb-stats/" in (repo / ".gitignore").read_text(encoding="utf-8")
    hook = (hooks_dir(repo) / "pre-commit").read_text(encoding="utf-8")
    assert "kb init" in hook and "precommit" in hook
    assert KB.resolve().as_posix() in hook
    assert not (hooks_dir(repo) / "pre-commit.local").exists()
    snap = {p: p.read_bytes() for p in [repo / ".gitignore", hooks_dir(repo) / "pre-commit"]}
    ok(repo, "init")
    assert {p: p.read_bytes() for p in snap} == snap
    assert not (hooks_dir(repo) / "pre-commit.local").exists()


def test_init_keeps_existing_ignore(repo):
    write(repo, ".gitignore", "/.claude/\n")
    ok(repo, "init")
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "/.claude/\n"


def test_init_chains_existing_hook(repo):
    """F15: the repo's own hook still runs first and still blocks."""
    hooks_dir(repo).mkdir(exist_ok=True)
    own = "#!/bin/sh\necho ran > own-hook-ran\nexit 1\n"
    (hooks_dir(repo) / "pre-commit").write_text(own, encoding="utf-8", newline="\n")
    ok(repo, "init")
    ok(repo, "init")
    assert (hooks_dir(repo) / "pre-commit.local").read_text(encoding="utf-8") == own
    assert "kb init" in (hooks_dir(repo) / "pre-commit").read_text(encoding="utf-8")
    write(repo, "h", "x\n")
    git(repo, "add", "h")
    p = commit(repo)
    assert p.returncode != 0
    assert (repo / "own-hook-ran").is_file()


def test_precommit_accepts_kb_writes(gated):
    ok(gated, "set", "a/one", "first, revised", "--from", "f")
    ok(gated, "set", "b/owned", "the owner said so", "--owner", "--pin")
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode == 0, p.stderr


def test_precommit_rejects(gated):
    claims = gated / "docs/claims"
    # A hand edit: the seal no longer matches.
    hand_edit(gated, "a/one", "edited by hand")
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode != 0 and "docs/claims/a/one.json" in p.stderr and "seal" in p.stderr
    git(gated, "reset", "-q", "--hard")
    # Conflict markers.
    (claims / "a/one.json").write_text("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n",
                                       encoding="utf-8")
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode != 0 and "docs/claims/a/one.json" in p.stderr and "JSON" in p.stderr
    git(gated, "reset", "-q", "--hard")
    # A valid fact at the wrong path.
    (claims / "a/copy.json").write_bytes((claims / "a/one.json").read_bytes())
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode != 0 and "docs/claims/a/copy.json" in p.stderr and "key" in p.stderr
    git(gated, "reset", "-q", "--hard")
    # A sealed active fact with no sources that isn't the owner's.
    KBMOD.write_fact(claims / "a/bare.json", {
        "key": "a/bare", "rev": 1, "value": "v", "status": "active", "sources": [],
        "owner": False, "pinned": False, "note": None, "by": SESSION, "at": "t",
        "confirms": [], "flags": [], "history": []})
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode != 0 and "docs/claims/a/bare.json" in p.stderr and "sources" in p.stderr


def test_precommit_never_refuses_for_size(gated):
    """K-nogate: writers are never refused for STATUS's size."""
    bulk(gated, 12, 25, value="x" * 60, pinned=True)
    git(gated, "add", "-A")
    p = commit(gated)
    assert p.returncode == 0, p.stderr
    assert len(ok(gated, "render")) <= KBMOD.CAP


def test_precommit_judges_staged_content_only(gated):
    """F13: another session's unstaged hand edit doesn't block a commit; a
    pathspec commit of that file does."""
    hand_edit(gated, "a/one", "edited by hand")       # unstaged
    ok(gated, "set", "a/two", "second, revised", "--from", "g")
    git(gated, "add", "docs/claims/a/two.json")
    p = commit(gated)
    assert p.returncode == 0, p.stderr
    ok(gated, "set", "a/two", "second, again", "--from", "g")
    p = commit(gated, "--", "docs/claims/a/two.json")
    assert p.returncode == 0, p.stderr
    p = commit(gated, "--", "docs/claims/a/one.json")
    assert p.returncode != 0 and "a/one.json" in p.stderr


def test_precommit_in_worktree(gated, tmp_path):
    """F14: the hook in git's common dir runs for worktree commits."""
    wt = tmp_path / "wt"
    git(gated, "worktree", "add", "-q", "-b", "side", str(wt))
    hand_edit(wt, "a/one", "edited by hand in a worktree")
    git(wt, "add", "-A")
    p = commit(wt)
    assert p.returncode != 0 and "a/one.json" in p.stderr


def test_merge_conflict_resolution(gated):
    """F14: a whole-side resolution commits; markers left in don't."""
    git(gated, "checkout", "-q", "-b", "other")
    ok(gated, "set", "a/one", "value from other", "--from", "f")
    git(gated, "commit", "-qam", "other")
    git(gated, "checkout", "-q", "-")
    ok(gated, "set", "a/one", "value from main", "--from", "f")
    git(gated, "commit", "-qam", "main")
    m = subprocess.run(["git", "merge", "-q", "other"], cwd=gated, capture_output=True, text=True)
    assert m.returncode != 0  # conflicts on a/one
    git(gated, "add", "docs/claims/a/one.json")  # markers left in
    p = commit(gated, msg="merge")
    assert p.returncode != 0 and "a/one.json" in p.stderr
    git(gated, "checkout", "other", "--", "docs/claims/a/one.json")  # their side, whole
    p = commit(gated, msg="merge")
    assert p.returncode == 0, p.stderr
    assert "value from other" in ok(gated, "get", "a/one")


def test_kb_leaves_raw_records_alone(repo):
    """F11: in the work tree, kb writes only docs/claims/ and .gitignore."""
    (repo / "docs").mkdir()
    write(repo, "docs/decisions.md", "# Decisions\n\n## One\n\n- a decision\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "decisions")
    before = (repo / "docs/decisions.md").read_bytes()
    ok(repo, "init")
    ok(repo, "set", "d/one", "a decision holds", "--from", "docs/decisions.md#One")
    ok(repo, "set", "d/two", "built on it", "--from", "d/one", "--check", "cat f", "--expect", "one")
    for args in [("get", "d/one"), ("check",), ("find", "decision"), ("render",),
                 ("flag", "d/one", "doubt"), ("confirm", "d/one", "re-read it"),
                 ("verify",), ("retire", "d/two", "gone"), ("precommit",)]:
        kb(repo, *args)
    assert (repo / "docs/decisions.md").read_bytes() == before
    changed = [l[3:] for l in git(repo, "status", "--porcelain", "-uall").splitlines()]
    assert changed and all(c.startswith("docs/claims/") or c == ".gitignore" for c in changed), changed


# --- step 8: session start (hook-start) ---

def hook_start(cwd, project_dir=None):
    """The real hook command, with or without CLAUDE_PROJECT_DIR."""
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=SESSION)
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run([sys.executable, "-I", str(KB), "hook-start"], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", env=env)


@pytest.fixture
def no_project_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


def test_hook_start_without_store(repo, tmp_path, monkeypatch, no_project_dir):
    """F16: no store, no output, no delay; not a git repo either."""
    p = hook_start(repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    # No delay: no git or other child process, no wait for stdin. Checked, not
    # timed: a busy machine made a 1 s limit flaky.
    real, spawned, waited = subprocess.Popen, [], []
    def popen(*a, **kw):
        spawned.append(a)
        return real(*a, **kw)
    monkeypatch.setattr(KBMOD.subprocess, "Popen", popen)
    monkeypatch.setattr(KBMOD, "hook_session", lambda: waited.append(1))
    r = kb(repo, "hook-start")
    assert (r.returncode, r.stdout, spawned, waited) == (0, "", [], [])
    plain = tmp_path / "plain"
    plain.mkdir()
    p = hook_start(plain)
    assert (p.returncode, p.stdout) == (0, "")


def test_hook_start_with_store(repo, tmp_path):
    ok(repo, "set", "product/net", "The 10-29 net is the product.", "--from", "f", "--pin")
    (repo / "sub").mkdir()
    p = hook_start(repo / "sub")
    assert p.returncode == 0 and p.stdout.startswith("# STATUS"), p.stdout + p.stderr
    assert "- product/net: The 10-29 net is the product." in p.stdout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    p = hook_start(elsewhere, project_dir=repo)
    assert "- product/net: The 10-29 net is the product." in p.stdout


def test_hook_start_never_fails(repo, monkeypatch, no_project_dir):
    ok(repo, "set", "a/x", "v", "--from", "f")
    (repo / "docs/claims/a/broken.json").write_text("{not json", encoding="utf-8")
    p = hook_start(repo)
    assert p.returncode == 0 and "- a/broken: " in p.stdout  # listed as suspect
    monkeypatch.setattr(KBMOD, "render_text", lambda st, cap=KBMOD.CAP: 1 / 0)
    r = kb(repo, "hook-start")
    assert r.returncode == 0
    assert r.stdout.startswith("kb: STATUS.md not rendered: ") and r.stdout.count("\n") == 1


def test_hook_start_stays_under_the_hook_limit(repo, no_project_dir):
    """Pinned facts past the room are trimmed; the hook prints at most CAP
    characters (Claude Code previews more)."""
    bulk(repo, 12, 25, value="x" * 60, pinned=True)
    r = kb(repo, "hook-start")
    assert r.returncode == 0 and len(r.stdout) <= KBMOD.CAP, len(r.stdout)
    assert r.stdout.startswith("# STATUS") and "pinned not shown: kb due" in r.stdout


def hook_start_raw(repo, stdin_open_for=0):
    """The real hook command as Claude Code runs it: no session id in the
    environment, stdin a pipe. Returns (stdout bytes, whether kb exited while
    stdin was still open); stdin stays open for up to `stdin_open_for` seconds."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    p = subprocess.Popen([sys.executable, "-I", str(KB), "hook-start"], cwd=repo, env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    exited = False
    if stdin_open_for:
        try:
            p.wait(timeout=stdin_open_for)
            exited = True
        except subprocess.TimeoutExpired:
            pass
    out, _ = p.communicate(b"", timeout=30)
    return out, exited


def test_hook_start_doesnt_wait_on_open_stdin(repo):
    """If Claude Code keeps the hook's stdin open, hook-start still finishes:
    git calls mustn't inherit the stdin a reader thread is blocked on. Asks
    whether kb exited before stdin closed, not how fast: a busy machine made a
    3 s limit flaky. The 60 s is only how long a hung kb gets."""
    ok(repo, "set", "a/x", "v", "--from", "f")
    out, exited = hook_start_raw(repo, stdin_open_for=60)
    assert out.startswith(b"# STATUS"), out
    assert exited, "kb waited for stdin to close"


def test_hook_start_writes_lf(repo):
    """Windows mustn't turn each newline into CRLF: the cap counts LF lines."""
    ok(repo, "set", "a/x", "v", "--from", "f", "--pin")
    out, _ = hook_start_raw(repo)
    assert out.startswith(b"# STATUS") and b"\r\n" not in out, out[:200]


# --- step 9: statistics (events.csv) and kb stats ---

import csv
import re


def events(repo):
    f = repo / ".claude/kb-stats/events.csv"
    with open(f, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], [dict(zip(rows[0], r)) for r in rows[1:]]


def test_events(repo, no_project_dir):
    """K-events: one row per command and per check run, with outcome,
    reason and missed-lookup words; header on a new file."""
    ok(repo, "set", "a/x", "the value", "--from", "f")
    ok(repo, "set", "a/c", "f says one", "--from", "f", "--check", "cat f", "--expect", "one")
    ok(repo, "find", "value")
    ok(repo, "find", "no", "such", "thing")
    refused(repo, "set", "a/y", "no sources")
    write(repo, "f", "one\n\n")  # same check output, new bytes: a/c is suspect, its check runs
    ok(repo, "check")
    ok(repo, "hook-start")
    header, rows = events(repo)
    assert header == KBMOD.EVENT_COLUMNS
    assert all(r["schema"] == str(KBMOD.EVENT_SCHEMA) and r["session"] == SESSION
               and r["worktree"] == "main" for r in rows)
    cmds = [r["command"] for r in rows]
    assert cmds == ["set", "check-run", "set", "find", "find", "set", "check-run", "check",
                    "hook-start"], cmds
    by = {i: r for i, r in enumerate(rows)}
    assert by[0]["keys"] == "a/x" and by[0]["outcome"] == "ok"
    assert by[1]["keys"] == "a/c" and by[1]["outcome"] == "match"
    assert by[3]["hits"] == "1" and by[3]["words"] == ""
    assert by[4]["hits"] == "0" and by[4]["words"] == "no such thing"
    assert by[5]["outcome"] == "refused" and "--from" in by[5]["reason"]
    assert by[6]["outcome"] == "match" and by[6]["keys"] == "a/c"
    assert (by[7]["facts"], by[7]["suspect"], by[7]["unchecked"]) == ("2", "1", "0")  # a/x cites f too
    assert by[8]["facts"] == "2" and int(by[8]["lines"]) > 5
    assert all(float(r["seconds"]) >= 0 for r in rows)


def test_no_events_without_store(repo, no_project_dir):
    ok(repo, "hook-start")
    kb(repo, "get", "a/x")  # not found, and still no store
    assert not (repo / ".claude").exists()


def test_events_from_worktree(repo, tmp_path):
    ok(repo, "init")  # ignores .claude/kb-stats/, so the worktree doesn't check it out
    ok(repo, "set", "a/x", "the value", "--from", "f")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "store")
    wt = tmp_path / "wt-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    ok(wt, "find", "value")
    assert not (wt / ".claude").exists()
    _, rows = events(repo)
    assert rows[-1]["command"] == "find" and rows[-1]["worktree"] == "wt-side"


def test_events_never_in_the_way(repo, no_project_dir):
    """Writing a row can't change output or exit code."""
    ok(repo, "set", "a/x", "the value", "--from", "f")
    cmds = [("get", "a/x"), ("find", "value"), ("find", "nothing"), ("check",), ("render",),
            ("get", "a/missing"), ("set", "a/y", "no sources")]
    before = [kb(repo, *c) for c in cmds]
    shutil.rmtree(repo / ".claude/kb-stats")
    (repo / ".claude/kb-stats").write_text("a file, not a folder", encoding="utf-8")
    after = [kb(repo, *c) for c in cmds]
    for c, b, a in zip(cmds, before, after):
        assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr), c


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def transcript_line(ts, name, **inp):
    return json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "s1",
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


@pytest.fixture
def projects(tmp_path, monkeypatch):
    d = tmp_path / "projects"
    d.mkdir()
    monkeypatch.setattr(KBMOD, "PROJECTS", d)
    return d


def test_stats_transcripts(repo, projects):
    """K-stats: one kb find, a Read of a source file before it (a re-open) and
    one after it (not), plus a fork's Read after the lookup."""
    ok(repo, "set", "a/x", "the value", "--from", "f")
    d = projects / slug(repo)
    (d / "s1" / "subagents").mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join([
        transcript_line("2026-11-02T10:00:00.000Z", "Read", file_path=str(repo / "f")),
        transcript_line("2026-11-02T10:01:00.000Z", "Bash", command="kb find value"),
        transcript_line("2026-11-02T10:02:00.000Z", "Read", file_path=str(repo / "f")),
        transcript_line("2026-11-02T10:03:00.000Z", "Bash", command="kb set a/z 'v' --from f"),
    ]) + "\n", encoding="utf-8")
    (d / "s1" / "subagents" / "agent-1.jsonl").write_text(
        transcript_line("2026-11-02T10:04:00.000Z", "Bash", command="cat f") + "\n", encoding="utf-8")
    out = ok(repo, "stats")
    assert "lookups 1, re-opens 1" in out, out
    out = ok(repo, "stats", "--since", "2026-11-03")
    assert "lookups 0, re-opens 0" in out, out


def test_stats_events(repo, projects):
    """K-stats, part 2: a fixture events file -> miss words, session-start times."""
    ok(repo, "set", "a/x", "the value", "--from", "f")
    f = repo / ".claude/kb-stats/events.csv"
    rows = [
        {"at": "2026-11-02T10:00:00+07:00", "command": "hook-start", "outcome": "ok", "seconds": "0.3",
         "facts": "5", "suspect": "1", "unchecked": "0", "lines": "20"},
        {"at": "2026-11-02T11:00:00+07:00", "command": "find", "outcome": "ok", "hits": "0",
         "words": "product net"},
        {"at": "2026-11-02T11:01:00+07:00", "command": "find", "outcome": "ok", "hits": "0",
         "words": "product net"},
        {"at": "2026-11-02T11:02:00+07:00", "command": "set", "outcome": "refused",
         "reason": "a fact needs --from <files or facts>, or --owner"},
        {"at": "2026-11-02T11:03:00+07:00", "command": "check-run", "outcome": "timeout", "seconds": "10.2"},
        {"at": "2026-11-02T12:00:00+07:00", "command": "hook-start", "outcome": "ok", "seconds": "0.9",
         "facts": "6", "suspect": "3", "unchecked": "1", "lines": "22"},
    ]
    with open(f, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, KBMOD.EVENT_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({"schema": KBMOD.EVENT_SCHEMA} | r)
    out = ok(repo, "stats")
    assert '"product net" x2' in out, out
    assert "median 0.6 s, slowest 0.9 s" in out, out
    assert "suspect 1 -> 3, unchecked 0 -> 1" in out, out
    assert "timeout 1" in out and "10.2 s" in out, out
    assert "a fact needs --from" in out, out


# --- Amendment 2: KB_READONLY ---

def snapshot(repo):
    """Every file under the repo, .git included, as bytes."""
    return {p.relative_to(repo).as_posix(): p.read_bytes()
            for p in repo.rglob("*") if p.is_file()}


def test_readonly_writes_nothing(repo, monkeypatch, no_project_dir):
    ok(repo, "set", "a/x", "one is in f", "--from", "f", "--check", "cat f", "--expect", "one")
    ok(repo, "check")  # leaves an events file and a hash cache behind
    (repo / "f").write_bytes(b"one\n\n")  # new bytes, same check output
    before = snapshot(repo)
    assert any(k.endswith("kb-hash-cache.json") for k in before)
    assert ".claude/kb-stats/events.csv" in before
    monkeypatch.setenv("KB_READONLY", "1")
    for args in (["get", "a/x"], ["find", "one"], ["check"], ["render"], ["stats"],
                 ["precommit"], ["hook-start"]):
        r = kb(repo, *args)
        assert r.returncode == 0, (args, r.stdout + r.stderr)
    assert state(repo, "a/x").startswith("suspect")  # the passing check didn't run
    for args in (["set", "a/y", "v", "--from", "f"], ["confirm", "a/x", "re-read f"],
                 ["flag", "a/x", "why"], ["retire", "a/x", "why"], ["init"], ["verify"]):
        r = kb(repo, *args)
        assert r.returncode == 1 and "KB_READONLY" in r.stderr, (args, r.stdout + r.stderr)
    after = snapshot(repo)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert changed == [], changed


# --- kb-curator: curating due, kb due, room for pins ---

def curate_file(repo):
    return repo / ".git" / "kb-curate.json"


def age(repo, key, hours):
    """Set back the time `key` was first seen not current."""
    f = curate_file(repo)
    d = json.loads(f.read_text(encoding="utf-8"))
    t = datetime.now().astimezone() - timedelta(hours=hours)
    d["suspect_since"][key] = t.isoformat(timespec="seconds")
    f.write_text(json.dumps(d), encoding="utf-8")


def test_render_room_for_pins_and_suspects(repo):
    """K-room: pins past the room are trimmed and named; Suspect keeps 2,000."""
    bulk(repo, 12, 25, value="x" * 60, pinned=True)
    bulk(repo, 1, 40, value="y" * 80, prefix="sus",
         flags=[{"by": SESSION, "at": "t", "reason": "the code says otherwise"}])
    out = ok(repo, "render")
    assert len(out) <= KBMOD.CAP, len(out)
    pin = out[out.index("## Pinned"):out.index("## Areas")].splitlines()
    m = re.fullmatch(r"\.\.\. and (\d+) pinned not shown: kb due", pin[-1])
    assert m and int(m.group(1)) > 0, pin[-1]
    k = int(m.group(1))
    assert len(pin) - 2 + k == 300
    due = out.splitlines()[2]
    assert due.startswith("Curating due: ") and f"{k} pinned not shown" in due
    assert "only on the facts your task touches" in due
    sus = out[out.index("## Suspect"):out.index("## Pinned")]
    assert len(sus) > 1800, len(sus)


def test_suspect_since(repo):
    """K-since: kb notes when a fact went suspect; over a day, curating is due."""
    ok(repo, "set", "a/x", "v", "--from", "f")
    write(repo, "f", "changed\n")
    ok(repo, "check")
    assert "a/x" in json.loads(curate_file(repo).read_text(encoding="utf-8"))["suspect_since"]
    assert "Curating due" not in ok(repo, "render")
    age(repo, "a/x", 25)
    out = ok(repo, "render")
    assert "Curating due: 1 not current for over a day. Run the curate skill" in out
    due = ok(repo, "due")
    assert "- a/x since " in due[:due.index("## Pinned")]
    ok(repo, "confirm", "a/x", "checked f")
    assert "Curating due" not in ok(repo, "render")
    assert json.loads(curate_file(repo).read_text(encoding="utf-8"))["suspect_since"] == {}


def test_due(repo):
    """K-due: overdue facts bottom up, the next area to sweep, --swept."""
    ok(repo, "set", "a/y", "base", "--from", "f")
    ok(repo, "set", "a/x", "built on a/y", "--from", "a/y")
    ok(repo, "set", "b/z", "other", "--owner")
    write(repo, "f", "changed\n")
    ok(repo, "check")
    age(repo, "a/y", 30)
    age(repo, "a/x", 30)
    out = ok(repo, "due")
    over = out[:out.index("## Pinned")].splitlines()
    assert over[0] == "## Overdue (2)"
    assert over[1].startswith("- a/y since ") and over[2].startswith("- a/x since ")
    assert out.splitlines()[-1] == "a"
    swept = out[out.index("## Last swept"):out.index("## Next area")].splitlines()
    assert swept[1:] == ["- a: never", "- b: never"]
    assert ok(repo, "due", "--swept", "a") == ""
    out = ok(repo, "due")
    assert out.splitlines()[-1] == "b"
    assert re.search(r"^- a: \d{4}-\d\d-\d\dT", out, re.M) and "- b: never" in out
    rows = [r for r in events(repo)[1] if r["command"] == "due"]
    assert rows[0]["hits"] == "2"


def test_due_readonly(repo, monkeypatch):
    """K-readonly: kb-curate.json doesn't change; --swept is refused."""
    ok(repo, "set", "a/x", "v", "--from", "f")
    write(repo, "f", "changed\n")
    monkeypatch.setenv("KB_READONLY", "1")
    ok(repo, "render")
    ok(repo, "due")
    assert not curate_file(repo).exists()
    r = kb(repo, "due", "--swept", "a")
    assert r.returncode == 1 and "KB_READONLY" in r.stderr
    monkeypatch.delenv("KB_READONLY")
    ok(repo, "check")
    before = curate_file(repo).read_bytes()
    monkeypatch.setenv("KB_READONLY", "1")
    age_free = ok(repo, "render")
    assert curate_file(repo).read_bytes() == before and "# STATUS" in age_free


def test_hook_start_adds_no_git_call(repo, no_project_dir, monkeypatch):
    """C3: with only --owner facts, session start doesn't ask git for its
    common dir (kb-curate.json sits there, but a main checkout's .git is it)."""
    ok(repo, "set", "a/x", "v", "--owner")
    calls, real = [], KBMOD.subprocess.run

    def spy(args, *a, **k):
        if args and args[0] == "git":
            calls.append(list(args[1:3]))
        return real(args, *a, **k)

    monkeypatch.setattr(KBMOD.subprocess, "run", spy)
    assert kb(repo, "hook-start").returncode == 0
    assert ["rev-parse", "--git-common-dir"] not in calls, calls


def test_long_values(repo):
    """K-long: kb set warns over 400 characters, never refuses; long facts
    count as curating due and kb due lists them."""
    r = kb(repo, "set", "a/x", "x" * 401, "--owner")
    assert r.returncode == 0
    assert "kb: warning: a/x is 401 characters, over 400" in r.stderr
    r = kb(repo, "set", "a/y", "y" * 400, "--owner")
    assert r.returncode == 0 and "warning" not in r.stderr
    assert "Curating due: 1 over 400 characters." in ok(repo, "render")
    out = ok(repo, "due")
    long = out[out.index("## Long (1)"):out.index("## Last swept")].splitlines()
    assert long[1:] == ["- a/x (401 chars)"]


# --- amendment 5: kb reseal ---

def store_bytes(repo):
    d = repo / "docs" / "claims"
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*.json")}


@pytest.fixture
def redacted(repo):
    """K-reseal's store, after the export's edits: a/x hand-edited, f and
    notes.md#B changed, g deleted."""
    write(repo, "notes.md", "# A\n\nalpha\n\n# B\n\nbeta\n")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-qm", "notes")
    ok(repo, "set", "a/x", "the secret is one", "--from", "f")
    ok(repo, "set", "a/x", "the secret is in f", "--from", "f")
    ok(repo, "set", "a/y", "beta is in B", "--from", "notes.md#B")
    ok(repo, "set", "a/z", "built on a/x", "--from", "a/x")
    ok(repo, "set", "a/m", "gee is in g", "--from", "g")
    ok(repo, "set", "a/r", "gone", "--from", "f")
    ok(repo, "retire", "a/r", "test")
    before = fact(repo, "a/x")
    p = repo / "docs/claims/a/x.json"
    p.write_text(p.read_text(encoding="utf-8").replace("secret", "[redacted]"), encoding="utf-8")
    write(repo, "f", "one, redacted\n")
    write(repo, "notes.md", "# A\n\nalpha\n\n# B\n\nbeta, redacted\n")
    (repo / "g").unlink()
    return repo, before


def test_reseal(redacted):
    repo, before = redacted
    assert kb(repo, "get", "a/x").returncode != 0  # bad seal
    out = ok(repo, "reseal")
    assert "resealed 1 of 5 facts (1 had a bad seal)" in out
    d = fact(repo, "a/x")
    assert d["value"] == "the [redacted] is in f"
    assert [h["value"] for h in d["history"]] == ["the [redacted] is one"]
    for k in ("rev", "by", "at", "confirms", "flags", "sources"):
        assert d.get(k) == before.get(k), k
    for key in ("a/x", "a/y", "a/z", "a/m"):
        assert state(repo, key).startswith("suspect"), key
    out = ok(repo, "reseal", "--sources")
    assert "updated 2 source hashes in 2 facts" in out
    assert "kept old hash: a/m: missing: g" in out
    for key in ("a/x", "a/y", "a/z"):
        assert state(repo, key) == "clean", key
    assert state(repo, "a/m").startswith("suspect")
    assert fact(repo, "a/x")["confirms"] == before.get("confirms")
    again = store_bytes(repo)
    ok(repo, "reseal", "--sources")
    assert store_bytes(repo) == again


def test_reseal_refuses(redacted, monkeypatch):
    repo, _ = redacted
    bad = repo / "docs/claims/a/y.json"
    good = bad.read_text(encoding="utf-8")
    bad.write_text("<<<<<<< HEAD\n" + good, encoding="utf-8")
    before = store_bytes(repo)
    assert "a/y.json" in refused(repo, "reseal")
    assert store_bytes(repo) == before
    bad.write_text(good, encoding="utf-8")
    moved = repo / "docs/claims/b/y.json"
    moved.parent.mkdir()
    moved.write_text(good, encoding="utf-8")
    before = store_bytes(repo)
    assert "b/y.json" in refused(repo, "reseal", "--sources")
    assert store_bytes(repo) == before
    moved.unlink()
    monkeypatch.setenv("KB_READONLY", "1")
    r = kb(repo, "reseal")
    assert r.returncode == 1 and "KB_READONLY" in r.stderr
    assert store_bytes(repo) == {k: v for k, v in before.items() if k != "b/y.json"}


def test_reseal_commits(gated):
    hand_edit(gated, "a/one", "first, redacted")
    ok(gated, "reseal")
    git(gated, "add", "-A")
    p = commit(gated, msg="resealed")
    assert p.returncode == 0, p.stderr
