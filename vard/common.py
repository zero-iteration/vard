#!/usr/bin/env python3
"""Small text helpers: extract identifier/filename seeds from a task, tokenize, etc."""
import os, re

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
BACKTICK_RE = re.compile(r"`([^`]+)`")
FENCED_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
TRACEBACK_RE = re.compile(r'File "([^"]+)", line \d+, in (\w+)')
DOTTED_RE = re.compile(r"\b[A-Za-z_]\w+(?:\.[A-Za-z_]\w+)+\b")
CALL_RE = re.compile(r"\b([A-Za-z_]\w{2,})\s*\(")
PATHLIKE_RE = re.compile(r"[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go)")
STOPWORDS = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "was", "one", "our", "out",
    "has", "this", "that", "with", "from", "have", "will", "when", "what", "which", "should", "would",
    "could", "there", "their", "about", "into", "than", "then", "them", "these", "those", "issue", "error",
    "code", "test", "tests", "file", "files", "function", "method", "class", "return", "value", "values",
    "expected", "actual", "following", "example", "https", "http", "com", "github"}


def subtokens(name):
    name = re.sub(r"[^A-Za-z0-9]", " ", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)   # split camelCase
    return [t.lower() for t in name.split() if len(t) >= 2]


def _prose_idents(s, allow_plain=False):
    out = set()
    for m in IDENT_RE.findall(s or ""):
        if m.lower() in STOPWORDS:
            continue
        pascal = m[0].isupper() and any(c.islower() for c in m[1:])
        camel = any(c.isupper() for c in m[1:]) and any(c.islower() for c in m)
        if pascal or camel or "_" in m or (allow_plain and len(m) >= 4):
            out.add(m)
    return out


def extract_seeds(problem_statement):
    """Identifiers + filenames mentioned in a task/bug report (title, code spans, tracebacks)."""
    text = problem_statement or ""
    idents, fnames = set(), set()
    lines = [l for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else ""
    for region in BACKTICK_RE.findall(text) + FENCED_RE.findall(text):
        idents.update(IDENT_RE.findall(region))
    for path, func in TRACEBACK_RE.findall(text):
        idents.add(func); fnames.add(os.path.basename(path))
    idents.update(CALL_RE.findall(text))
    for d in DOTTED_RE.findall(text):
        for seg in d.split(".")[1:]:
            if len(seg) >= 3:
                idents.add(seg)
    idents |= _prose_idents(text)
    idents |= _prose_idents(title, allow_plain=True)
    for m in PATHLIKE_RE.findall(text):
        fnames.add(os.path.basename(m)); fnames.add(m.lstrip("./"))
    idents = {i for i in idents if len(i) >= 2 and i.lower() not in STOPWORDS}
    return idents, fnames


def task_text(problem_statement):
    return (problem_statement or "")[:2000]
