#!/usr/bin/env python3
"""Discover the full source footprint to index: the whole multi-module project (Maven reactor /
Gradle root), not just the directory you happened to point at, plus any extra source roots.

Running `vard init` inside one module of a Spring project would otherwise miss the sibling modules it
depends on — so the agent gets a partial picture. This walks up to the project root and (best-effort)
finds co-located source dependencies.
"""
import os, re, subprocess, json
from .languages.profiles import _PROFILES

_MAX_UP = 6
_GRADLE_SETTINGS = ("settings.gradle", "settings.gradle.kts")
# Build/module markers across ALL languages, so module discovery works on Python/JS/Go repos too — not
# just Maven/Gradle. Sourced from each LanguageProfile so adding a language adds its build files for free.
_BUILD_FILES = tuple({f for p in _PROFILES for f in p.build_files})
_ROOT_MARKERS = tuple({f for p in _PROFILES for f in p.root_marker_files})
_MODULE_FILES = ("pom.xml", "build.gradle", "build.gradle.kts",          # Java
                 "pyproject.toml", "setup.py",                            # Python
                 "package.json",                                          # JS/TS
                 "go.mod")                                                # Go


def _git_root(path):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                           capture_output=True, text=True, timeout=10).stdout.strip()
        return r or None
    except Exception:
        return None


def _has_build(d):
    return any(os.path.isfile(os.path.join(d, f)) for f in _BUILD_FILES)


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
        # the gradle settings root). A root-marker file (gradle settings, go.work, pnpm-workspace,
        # lerna/nx) marks the definitive outermost root.
        if any(os.path.isfile(os.path.join(parent, s)) for s in _ROOT_MARKERS):
            return parent
        if _has_build(parent):
            root = parent
            cur = parent
        else:
            break
    return root


def find_modules(root):
    """Module dirs under the root (each has its own build file). For reporting coverage. Recognizes
    Maven/Gradle, Python (pyproject/setup.py), JS/TS (package.json) and Go (go.mod) modules."""
    mods = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "target", "build", "node_modules", ".venv", ".vard", ".idea", "dist")]
        if dirpath != root and any(f in files for f in _MODULE_FILES):
            mods.append(os.path.relpath(dirpath, root))
            dirs[:] = [d for d in dirs if d != "src"]   # don't descend into a module's src for sub-listing
    return sorted(mods)


_MVN_ART = re.compile(r"<artifactId>\s*([\w.\-]+)\s*</artifactId>")


def _read(p):
    try:
        return open(p, errors="ignore").read()
    except Exception:
        return ""


def _module_id(d):
    """A module's OWN identity, tagged by ecosystem, for matching against another module's declared deps:
    Maven artifactId / npm package name / Go module path / Python project name. Dir-name-independent."""
    pom = os.path.join(d, "pom.xml")
    if os.path.isfile(pom):
        m = _MVN_ART.search(re.sub(r"<parent>.*?</parent>", "", _read(pom), flags=re.S))   # skip <parent>
        if m:
            return ("mvn", m.group(1))
    pkg = os.path.join(d, "package.json")
    if os.path.isfile(pkg):
        try:
            j = json.loads(_read(pkg))
            if j.get("name"):
                return ("js", j["name"])
        except Exception:
            pass
    gomod = os.path.join(d, "go.mod")
    if os.path.isfile(gomod):
        m = re.search(r"^\s*module\s+(\S+)", _read(gomod), re.M)
        if m:
            return ("go", m.group(1))
    for pyf, rx in ((os.path.join(d, "pyproject.toml"), r'(?m)^\s*name\s*=\s*["\']([^"\']+)'),
                    (os.path.join(d, "setup.py"), r'name\s*=\s*["\']([^"\']+)')):
        if os.path.isfile(pyf):
            m = re.search(rx, _read(pyf))
            if m:
                return ("py", m.group(1).lower())
    return None


def _declared_deps(d):
    """Dependency identifiers a module DECLARES (ecosystem-tagged), to match against sibling module ids."""
    out = set()
    pom = os.path.join(d, "pom.xml")
    if os.path.isfile(pom):
        out |= {("mvn", a) for a in _MVN_ART.findall(_read(pom))}
    pkg = os.path.join(d, "package.json")
    if os.path.isfile(pkg):
        try:
            j = json.loads(_read(pkg))
            for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                out |= {("js", name) for name in (j.get(sec) or {})}
        except Exception:
            pass
    gomod = os.path.join(d, "go.mod")
    if os.path.isfile(gomod):
        out |= {("go", m) for m in re.findall(r"^\s*(?:require\s+)?([\w./\-]+)\s+v\d", _read(gomod), re.M)}
    for pyf in (os.path.join(d, "pyproject.toml"), os.path.join(d, "setup.py")):
        if os.path.isfile(pyf):
            for blk in re.findall(r'(?:dependencies|install_requires)\s*=\s*\[(.*?)\]', _read(pyf), re.S):
                out |= {("py", dep.lower()) for dep in re.findall(r'["\']([A-Za-z0-9_.\-]+)', blk)}
    return out


def discover_source_deps(root, search_dirs=None):
    """Local source of a project's DECLARED dependencies (cross-ecosystem: Maven/Gradle artifactId, npm
    package name, Go module path, Python project name). Collects what the project tree declares, then matches
    a sibling by its OWN module identity — robust to dir-name != module-name — descending one level into
    sibling reactors/workspaces. Nothing here is repo-specific; everything is resolved from the given root."""
    declared = set()
    for dp, dirs, _ in os.walk(root):
        dirs[:] = [x for x in dirs if x not in
                   (".git", "target", "build", "node_modules", ".venv", ".vard", ".idea", "dist")]
        if dp.count(os.sep) - root.count(os.sep) <= 3:
            declared |= _declared_deps(dp)
    if not declared:
        return []
    rootabs = os.path.abspath(root)
    found = []

    def consider(d):
        if os.path.abspath(d) == rootabs or not os.path.isdir(d):
            return
        mid = _module_id(d)
        if mid and mid in declared and _has_build(d):
            found.append(os.path.abspath(d))

    for base in (search_dirs or [os.path.dirname(rootabs)]):
        try:
            entries = os.listdir(base)
        except Exception:
            continue
        for name in entries:
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            consider(d)                                   # the sibling itself
            if _has_build(d):                             # sibling is a reactor/workspace -> descend 1 level
                try:
                    for sub in os.listdir(d):
                        consider(os.path.join(d, sub))
                except Exception:
                    pass
    return sorted(set(found))
