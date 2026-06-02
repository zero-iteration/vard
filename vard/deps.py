#!/usr/bin/env python3
"""Discover the full source footprint to index: the whole multi-module project (Maven reactor /
Gradle root), not just the directory you happened to point at, plus any extra source roots.

Running `vard init` inside one module of a Spring project would otherwise miss the sibling modules it
depends on — so the agent gets a partial picture. This walks up to the project root and (best-effort)
finds co-located source dependencies.
"""
import os, re, subprocess

_MAX_UP = 6
_GRADLE_SETTINGS = ("settings.gradle", "settings.gradle.kts")


def _git_root(path):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                           capture_output=True, text=True, timeout=10).stdout.strip()
        return r or None
    except Exception:
        return None


def _has_build(d):
    return (os.path.isfile(os.path.join(d, "pom.xml"))
            or any(os.path.isfile(os.path.join(d, s)) for s in _GRADLE_SETTINGS)
            or os.path.isfile(os.path.join(d, "build.gradle"))
            or os.path.isfile(os.path.join(d, "build.gradle.kts")))


def find_project_root(path):
    """Walk up to the outermost build root (the Maven reactor / Gradle root project), bounded by the
    git toplevel so we never escape the repo. Returns the dir to index from."""
    path = os.path.abspath(path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    ceiling = _git_root(path)
    root = path
    cur = path
    for _ in range(_MAX_UP):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        if ceiling and not parent.startswith(ceiling):
            break
        # the parent is part of the same build if it ALSO has a build file (chain of reactor poms /
        # the gradle settings root). A gradle settings file marks the definitive root.
        if any(os.path.isfile(os.path.join(parent, s)) for s in _GRADLE_SETTINGS):
            return parent
        if _has_build(parent):
            root = parent
            cur = parent
        else:
            break
    return root


def find_modules(root):
    """Module dirs under the root (each has its own build file). For reporting coverage."""
    mods = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "target", "build", "node_modules", ".venv", ".vard", ".idea")]
        if dirpath != root and ("pom.xml" in files or "build.gradle" in files or "build.gradle.kts" in files):
            mods.append(os.path.relpath(dirpath, root))
            dirs[:] = [d for d in dirs if d != "src"]   # don't descend into a module's src for sub-listing
    return sorted(mods)


_DEP = re.compile(r"<artifactId>\s*([\w.\-]+)\s*</artifactId>")


def discover_source_deps(root, search_dirs=None):
    """Best-effort: source of DECLARED dependencies that lives locally outside the reactor — a sibling
    workspace project whose artifactId matches a dependency. Returns extra source-root dirs. Conservative:
    only sibling dirs of the project root, only Java/Gradle projects, only matching declared artifactIds."""
    artifacts = set()
    for dp, _, fs in os.walk(root):
        if "pom.xml" in fs:
            try:
                artifacts |= set(_DEP.findall(open(os.path.join(dp, "pom.xml"), errors="ignore").read()))
            except Exception:
                pass
        if dp.count(os.sep) - root.count(os.sep) > 3:
            continue
    if not artifacts:
        return []
    siblings = search_dirs or [os.path.dirname(os.path.abspath(root))]
    found = []
    for base in siblings:
        try:
            for name in os.listdir(base):
                d = os.path.join(base, name)
                if d == os.path.abspath(root) or not os.path.isdir(d):
                    continue
                if name in artifacts and _has_build(d):
                    found.append(d)
        except Exception:
            continue
    return found
