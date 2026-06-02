"""Adapter: ContextBench (Multi-SWE-Bench / SWE-PolyBench) -> our Bug objects.

Lets us run codefirst/hybrid/state_lineage through the SAME dark-gold harness on a trusted,
externally-curated benchmark (not hand-picked by us). gold_context is the annotated gold spans.

Note on expected outcome: the Java subset is mostly libraries/frameworks (fastjson2, etc.) where
shared-state coupling is rare, so the state layer should mostly stay inert and hybrid should ==
codefirst (a no-harm check on external data), with dark gold appearing only where it genuinely exists.
"""
import json, os, subprocess
from . import dataset as D

CB_DIR = os.path.expanduser("~/Desktop/vard-bench/contextbench/data")


def _checkout_or_fetch(repo_dir, commit):
    """A full clone only has the default branch; ContextBench base commits may live on other refs.
    Try a normal checkout, and if the commit object is missing, fetch that exact SHA then retry."""
    try:
        D._checkout(repo_dir, commit)
        return True
    except Exception:
        subprocess.run(["git", "fetch", "--quiet", "origin", commit], cwd=repo_dir,
                       capture_output=True, text=True)
        D._checkout(repo_dir, commit)            # raises if still missing -> caller skips this bug
        return True


def load_cb_bugs(split="contextbench_verified", lang="java", limit=None, max_repo_mb=400):
    import pyarrow.dataset as ds
    rows = ds.dataset(os.path.join(CB_DIR, f"{split}.parquet"), format="parquet").to_table().to_pylist()
    rows = [r for r in rows if r.get("language") == lang]
    if limit:
        rows = rows[:limit]
    bugs = []
    for r in rows:
        try:
            name = r["repo"].replace("/", "__")
            url = r.get("repo_url") or f"https://github.com/{r['repo']}"
            repo_dir = D._ensure_repo(url, name)
            _checkout_or_fetch(repo_dir, r["base_commit"])
            gold = []
            for g in json.loads(r["gold_context"]):
                gold.append(D.Span(g["file"], int(g["start_line"]), int(g["end_line"])))
            bugs.append(D.Bug(
                id=r["instance_id"][:48], issue_text=r["problem_statement"] or "",
                bug_class="contextbench", repo_dir=os.path.abspath(repo_dir), gold=gold,
                repo_url=url, base_commit=r["base_commit"]))
        except Exception as e:
            print(f"  ! skip {r['instance_id'][:40]}: {str(e)[:70]}")
    return bugs
