"""Tree-sitter provider: symbol + call-site extraction across languages.

Add a language by adding an entry to LANGS (which CST node types are containers /
functions / calls / imports / decorators). Name/base/arg extraction is generic with
small per-grammar field fallbacks.
"""
import re
from .base import Symbol, CallSite, FileArtifact, LanguageProvider

LANGS = {
    "python":     {"ext": [".py", ".pyi"],
                   "container": ["class_definition"], "func": ["function_definition"],
                   "call": ["call"], "new": [], "import": ["import_statement", "import_from_statement"],
                   "decorator": ["decorator"]},
    "java":       {"ext": [".java"],
                   "container": ["class_declaration", "interface_declaration", "enum_declaration", "record_declaration"],
                   "func": ["method_declaration", "constructor_declaration"],
                   "call": ["method_invocation"], "new": ["object_creation_expression"],
                   "import": ["import_declaration"], "decorator": ["annotation", "marker_annotation"]},
    "javascript": {"ext": [".js", ".jsx", ".mjs", ".cjs"],
                   "container": ["class_declaration", "class"], "func": ["function_declaration", "method_definition", "generator_function_declaration"],
                   "call": ["call_expression"], "new": ["new_expression"], "import": ["import_statement"],
                   "decorator": ["decorator"]},
    "typescript": {"ext": [".ts"],
                   "container": ["class_declaration", "interface_declaration", "abstract_class_declaration"],
                   "func": ["function_declaration", "method_definition", "method_signature"],
                   "call": ["call_expression"], "new": ["new_expression"], "import": ["import_statement"],
                   "decorator": ["decorator"]},
    "tsx":        {"ext": [".tsx"],
                   "container": ["class_declaration", "interface_declaration", "abstract_class_declaration"],
                   "func": ["function_declaration", "method_definition"],
                   "call": ["call_expression"], "new": ["new_expression"], "import": ["import_statement"],
                   "decorator": ["decorator"]},
    "go":         {"ext": [".go"],
                   "container": ["type_declaration"], "func": ["function_declaration", "method_declaration"],
                   "call": ["call_expression"], "new": [], "import": ["import_declaration"], "decorator": []},
}
_VAR_FUNC = {"arrow_function", "function_expression", "function"}   # JS/TS funcs named via their declarator


def _txt(n):
    if n is None:
        return ""
    t = n.text
    return t.decode("utf-8", "ignore") if isinstance(t, (bytes, bytearray)) else str(t)


def _name(node):
    nm = node.child_by_field_name("name")
    if nm is not None:
        return _txt(nm)
    for c in node.children:
        if c.type in ("identifier", "type_identifier", "property_identifier"):
            return _txt(c)
    return None


def _bases(node):
    out = []
    for fld in ("superclass", "interfaces", "superclasses", "class_heritage", "extends", "type"):
        ch = node.child_by_field_name(fld)
        if ch is not None:
            for d in _descend(ch):
                if d.type in ("identifier", "type_identifier", "scoped_type_identifier"):
                    out.append(_txt(d).split(".")[-1])
    return list(dict.fromkeys(out))


def _descend(n):
    yield n
    for c in n.children:
        yield from _descend(c)


def _decorators(node):
    """Annotations/decorators on a declaration (Java modifiers node / py decorated_definition / direct)."""
    scan = []
    scan += [c for c in node.children if c.type in ("annotation", "marker_annotation", "decorator")]
    for c in node.children:                                   # Java: annotations live under `modifiers`
        if c.type == "modifiers":
            scan += [g for g in c.children if g.type in ("annotation", "marker_annotation")]
    # python `decorated_definition` and JS/TS `export_statement` wrap the decl with the decorators as
    # siblings; also catch a bare decorator immediately preceding the decl within the same parent.
    if node.parent is not None and node.parent.type in ("decorated_definition", "export_statement"):
        scan += [c for c in node.parent.children if c.type == "decorator"]
    seen, out = set(), []
    for c in scan:
        v = _txt(c).lstrip("@")
        if v and v not in seen:
            seen.add(v); out.append(v)
    return out


def _callee(node):
    """Return (receiver, method) for a call/new node, across grammars."""
    if node.type in ("method_invocation",):                       # java
        return _txt(node.child_by_field_name("object")), _txt(node.child_by_field_name("name"))
    if node.type in ("object_creation_expression",):             # java new
        return "", _txt(node.child_by_field_name("type")).split(".")[-1]
    if node.type in ("new_expression",):                         # js new
        c = node.child_by_field_name("constructor"); return "", (_txt(c).split(".")[-1] if c else "")
    fn = node.child_by_field_name("function")
    if fn is None:
        return "", ""
    if fn.type in ("member_expression", "attribute", "selector_expression", "field_access"):
        obj = fn.child_by_field_name("object") or fn.child_by_field_name("operand") or fn.child_by_field_name("value")
        prop = (fn.child_by_field_name("property") or fn.child_by_field_name("attribute")
                or fn.child_by_field_name("field") or fn.child_by_field_name("name"))
        return _txt(obj), _txt(prop)
    return "", _txt(fn).split(".")[-1]


def _arg0(node):
    args = node.child_by_field_name("arguments") or node.child_by_field_name("argument_list")
    if args is None:
        return None
    for c in args.children:
        if c.type not in ("(", ")", ",", "{", "}"):
            return _txt(c)[:120]
    return None


class TreeSitterProvider(LanguageProvider):
    def __init__(self, lang):
        self.lang = lang
        self.cfg = LANGS[lang]
        self.extensions = set(self.cfg["ext"])
        self._parser = None

    def _parser_for(self):
        if self._parser is None:
            from tree_sitter_language_pack import get_parser
            self._parser = get_parser(self.lang)
        return self._parser

    def parse(self, repo_dir, rel, src):
        cfg = self.cfg
        art = FileArtifact(file=rel, lang=self.lang)
        try:
            parser = self._parser_for()
            try:
                tree = parser.parse(src.encode("utf-8", "ignore"))   # most tree-sitter builds
            except TypeError:
                tree = parser.parse(src)                              # builds that want str
        except Exception:
            return art
        cont, func, calls, news, imps, decs = (set(cfg["container"]), set(cfg["func"]), set(cfg["call"]),
                                               set(cfg["new"]), set(cfg["import"]), set(cfg["decorator"]))
        mod_id = "<module>"

        def walk(node, qual_stack):
            cur_qual = ".".join(qual_stack) if qual_stack else mod_id
            t = node.type
            handled_children = None
            if t in cont or t in func:
                nm = _name(node)
                if nm:
                    q = ".".join(qual_stack + [nm]) if qual_stack else nm
                    is_method = t in func and bool(qual_stack)
                    sym = Symbol(qual=q, type=("class" if t in cont else ("method" if is_method else "function")),
                                 file=rel, start=node.start_point[0] + 1, end=node.end_point[0] + 1,
                                 name=nm, parent_qual=(".".join(qual_stack) if qual_stack else None),
                                 bases=_bases(node) if t in cont else [], decorators=_decorators(node))
                    art.symbols.append(sym)
                    for c in node.children:
                        walk(c, qual_stack + [nm])
                    handled_children = True
            elif t == "variable_declarator":                      # JS/TS: const f = () => {...}
                val = node.child_by_field_name("value")
                nm = _name(node)
                if val is not None and val.type in _VAR_FUNC and nm:
                    q = ".".join(qual_stack + [nm]) if qual_stack else nm
                    art.symbols.append(Symbol(qual=q, type="function", file=rel, start=node.start_point[0] + 1,
                                              end=node.end_point[0] + 1, name=nm,
                                              parent_qual=(".".join(qual_stack) if qual_stack else None)))
                    for c in val.children:
                        walk(c, qual_stack + [nm])
                    handled_children = True
            if (t in calls) or (t in news):
                recv, meth = _callee(node)
                art.calls.append(CallSite(enclosing_qual=cur_qual, receiver=recv.strip(), method=meth.strip(),
                                          arg0=_arg0(node), line=node.start_point[0] + 1, file=rel))
            elif t in imps:
                art.imports.append(("", _txt(node)[:160]))
            if handled_children is None:
                for c in node.children:
                    walk(c, qual_stack)

        walk(tree.root_node, [])
        self._collect_var_types(tree.root_node, art)
        # module node spanning whole file
        end = max([s.end for s in art.symbols], default=1)
        art.symbols.insert(0, Symbol(qual=mod_id, type="module", file=rel, start=1, end=end, name=mod_id))
        return art

    def _collect_var_types(self, root, art):
        """Map variable/field name -> declared type (for precise DB resource identity)."""
        DECL = {"field_declaration", "formal_parameter", "required_parameter", "optional_parameter",
                "public_field_definition", "property_signature", "local_variable_declaration"}
        for n in _descend(root):
            if n.type not in DECL:
                continue
            ty = n.child_by_field_name("type")
            tytxt = re.sub(r"<.*>", "", _txt(ty)).strip().split(".")[-1] if ty is not None else None
            if not tytxt:
                continue
            for d in _descend(n):
                if d.type in ("variable_declarator", "identifier"):
                    nm = _name(d) if d.type == "variable_declarator" else _txt(d)
                    if nm and nm != tytxt:
                        art.var_types.setdefault(nm, tytxt)
