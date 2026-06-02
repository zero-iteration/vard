"""Bug dataset: manifests + gold reconstruction from git.

A bug is a pre-registered JSON manifest (see eval/bugs/_TEMPLATE.json). Gold = the spans
the accepted fix actually changed, reconstructed from `git diff base..fix` on the BASE
commit (the lines that existed before the fix and had to be found). Curation is yours:
you supply repo + two SHAs + issue text + bug_class; this turns that into gold spans.

Selection protocol (to neutralize cherry-pick / memorization, per the eval README):
  - pick BEFORE looking at any retriever output
  - obscure, actively-maintained, non-tiny Java/Spring repos
  - fix touches >=2 files
  - label bug_class: "coupling" (state write on one side, read on another, no direct call)
    vs "logic" (control/computation, the negative control)
"""
import json, os, re, subprocess
from dataclasses import dataclass, field

REPO_CACHE = os.path.expanduser(os.environ.get("VARD_EVAL_REPOS", "~/.vard-eval/repos"))

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Span:
    file: str
    start: int
    end: int


@dataclass
class Bug:
    id: str
    issue_text: str
    bug_class: str
    repo_dir: str                       # local path to the repo checked out at base_commit
    gold: list = field(default_factory=list)   # [Span]
    repo_url: str = ""
    base_commit: str = ""
    fix_commit: str = ""
    notes: str = ""


def _run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout


def _ensure_repo(url, name):
    os.makedirs(REPO_CACHE, exist_ok=True)
    dest = os.path.join(REPO_CACHE, name)
    if not os.path.isdir(os.path.join(dest, ".git")):
        print(f"  cloning {url} -> {dest}")
        subprocess.run(["git", "clone", "--quiet", url, dest], check=True)
    return dest


def _checkout(repo_dir, commit):
    subprocess.run(["git", "checkout", "--quiet", "--force", commit], cwd=repo_dir, check=True)


def gold_from_diff(repo_dir, base, fix, exts=(".java",)):
    """Base-side line ranges that the fix changed. Added-only hunks anchor at the
    insertion line (start==end). New files (no base side) are skipped with a note."""
    diff = _run(["git", "diff", f"{base}", f"{fix}", "--unified=0", "--", *(f"*{e}" for e in exts)],
                cwd=repo_dir)
    spans, cur_a = [], None
    skipped_new = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            cur_a = None
        elif line.startswith("--- "):
            p = line[4:].strip()
            cur_a = None if p == "/dev/null" else re.sub(r"^a/", "", p)
            if p == "/dev/null":
                skipped_new += 1
        elif line.startswith("@@") and cur_a:
            m = _HUNK.match(line)
            if not m:
                continue
            a, b = int(m.group(1)), (int(m.group(2)) if m.group(2) else 1)
            if b == 0:                                  # pure insertion: anchor at the line before
                spans.append(Span(cur_a, max(a, 1), max(a, 1)))
            else:
                spans.append(Span(cur_a, a, a + b - 1))
    return spans, skipped_new


def load_bug(path):
    """Load a manifest, materialize the repo at base_commit, derive gold if not explicit."""
    m = json.load(open(path))
    bid = m.get("id") or os.path.splitext(os.path.basename(path))[0]
    repo_dir = m.get("repo_dir")
    if repo_dir:
        repo_dir = os.path.expanduser(repo_dir)
    if not repo_dir:
        name = m.get("name") or re.sub(r"[^A-Za-z0-9]+", "_", m["repo_url"].rstrip("/").split("/")[-1])
        repo_dir = _ensure_repo(m["repo_url"], name)
    if m.get("base_commit"):
        _checkout(repo_dir, m["base_commit"])
    if m.get("gold"):
        gold = [Span(g["file"], int(g["start"]), int(g["end"])) for g in m["gold"]]
    else:
        gold, skipped = gold_from_diff(repo_dir, m["base_commit"], m["fix_commit"])
        if skipped:
            print(f"  [{bid}] note: {skipped} new file(s) in the fix have no base-side gold (skipped)")
    return Bug(id=bid, issue_text=m["issue_text"], bug_class=m.get("bug_class", "unknown"),
               repo_dir=os.path.abspath(repo_dir), gold=gold, repo_url=m.get("repo_url", ""),
               base_commit=m.get("base_commit", ""), fix_commit=m.get("fix_commit", ""),
               notes=m.get("notes", ""))


def load_bugs(paths):
    out = []
    for p in paths:
        try:
            out.append(load_bug(p))
        except Exception as e:
            print(f"  ! skip {p}: {e}")
    return out
