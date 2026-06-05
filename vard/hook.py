#!/usr/bin/env python3
"""VARD PreToolUse hook (Edit|Write) — proactive blast-radius warning.

When the agent is about to edit a function coupled through shared data (cache/DB/queue),
this surfaces the impact as advisory context (non-blocking). Fail-silent by design: any
error or un-indexed repo → exit 0 with no output, so it can NEVER break editing.

Register in settings.json:
  "hooks": {"PreToolUse": [{"matcher": "Edit|Write",
            "hooks": [{"type": "command", "command": "/path/to/vard-hook"}]}]}
"""
import json, os, sys


def _find_repo(start):
    d = os.path.abspath(start)
    if os.path.isfile(d):
        d = os.path.dirname(d)
    while True:
        if os.path.isfile(os.path.join(d, ".vard", "index.pkl")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


def _edit_line(file_path, ti):
    old = ti.get("old_string") or ti.get("oldString")
    if not old:
        return None
    try:
        text = open(file_path, encoding="utf-8", errors="ignore").read()
        i = text.find(old.split("\n")[0].strip() or old[:40])
        if i >= 0:
            return text[:i].count("\n") + 1
    except Exception:
        pass
    return None


def _userprompt(payload):
    """UserPromptSubmit: deterministically (1) inject fresh memories relevant to the prompt, and
    (2) capture an explicit user assertion as a code-anchored memory. Load-only; fail-silent."""
    import re
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return
    repo = _find_repo(payload.get("cwd") or ".")
    if not repo:
        return
    from vard import cli, query as Q, memory as MEM
    idx = cli.load_index(repo)                       # never rebuild inside a hook
    if not idx:
        return
    names = list({t for t in re.findall(r'\b[A-Z][A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?\b', prompt)})[:10]
    anchors = set()
    for t in names:
        for i in Q.resolve_target(idx, t)[:3]:
            anchors.add(i)
    # (2) capture: explicit assertion + a resolvable anchor -> remember (high precision, key-free)
    if re.search(r'\b(remember|note that|keep in mind|for the record|always|never|actually|the reason|'
                 r'gotcha|don.?t forget|FYI)\b', prompt, re.I) and anchors:
        try:
            MEM.remember(idx, repo, prompt[:300], list(anchors), source="hook")
        except Exception:
            pass
    # (1) recall: inject fresh, relevant memories as context
    try:
        rg = idx["rg"]
        files = {rg.nodes[a].file for a in anchors if a in rg.nodes}
        mtxt = MEM.recall_text(idx, repo, anchors=anchors, files=files, query=prompt)
    except Exception:
        mtxt = ""
    if mtxt:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": mtxt}}))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if payload.get("hook_event_name") == "UserPromptSubmit" or ("prompt" in payload and "tool_input" not in payload):
        try:
            _userprompt(payload)
        except Exception:
            return
        return
    try:
        ti = payload.get("tool_input", {}) or {}
        fp = ti.get("file_path") or ti.get("filePath")
        if not fp:
            return
        repo = _find_repo(payload.get("cwd") or fp) or _find_repo(fp)
        if not repo:
            return
        from vard import cli, query
        idx = cli.load_index(repo)              # load only — never rebuild inside a hook
        if not idx:
            return
        rg = idx["rg"]
        rel = os.path.relpath(os.path.abspath(fp), repo)
        if rel not in rg.by_file:
            return

        line = _edit_line(fp, ti)
        if line:
            ns = [n for n in rg.by_file[rel] if n.start <= line <= n.end and n.type != "module"]
            ns.sort(key=lambda n: n.end - n.start)
            targets = [ns[0].id] if ns else []
        else:                                   # Write / can't locate line → whole file
            targets = [n.id for n in rg.by_file[rel] if n.type != "module"]

        warnings, seen = [], set()
        for tid in targets[:6]:
            for it in query.impact(idx, tid).get("items", []):
                if it["relation"] in ("downstream", "upstream") and it["qual"] not in seen:
                    seen.add(it["qual"]); warnings.append(it)
        if not warnings:
            return
        lines = ["⚠️ VARD — this code is coupled through shared data. Review these before changing it:"]
        for it in warnings[:6]:
            lines.append(f"  • {it['qual']} ({it['loc']}) — {it['reason']}")
        lines.append("  → call vard_impact for the full blast radius.")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "additionalContext": "\n".join(lines)}}))
    except Exception:
        return                                  # never block an edit


if __name__ == "__main__":
    main()
