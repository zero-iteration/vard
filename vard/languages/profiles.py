"""Per-language HEURISTIC profiles — the upper-half analogue of the tree-sitter providers.

The graph/parse layer (providers) is already language-agnostic. The state layer, module discovery, and
candidate anchors used to hardcode Java/Spring conventions (the `*DTO`/`*ServiceImpl` naming, `new T(`
construction, `@Configuration` anchors, Maven `pom.xml`). A `LanguageProfile` factors all of that out:
one profile per language, resolved by file extension (per-file) or by repo-dominant language (repo-level
decisions like module discovery). Adding a language = add a profile here; nothing above needs to change.

The Java profile reproduces the original regexes EXACTLY so Java behaviour is unchanged. The Python / JS-TS
/ Go profiles use each language's real conventions (attribute assignment instead of `.setX()`, type
annotations / structs instead of `*DTO` suffixes, decorators / config modules instead of Spring anns).
"""
import os, re

_CAP = re.compile(r'\b([A-Z][A-Za-z0-9_]+)\b')


class LanguageProfile:
    lang = ""
    exts = ()
    # --- type-name classification (state vs infra/service) ---
    infra_re = re.compile(r'$^')         # type names that are scaffolding, never "state"
    data_like_re = re.compile(r'$^')     # type names that are clearly DATA (rank first as state)
    nondata_re = re.compile(r'$^')       # type names that are clearly behaviour (rank last)
    logger_re = re.compile(r'$^')        # logger-ish field types to skip as static-state noise
    data_dirs = ()                       # path fragments that mark a data/domain package
    svc_dirs = ()                        # path fragments that mark a service/infra package
    # --- producer signals (constructs / mutates a type) ---
    new_re = re.compile(r'$^')           # construction expression -> produced type name
    builder_re = re.compile(r'$^')       # builder expression -> produced type name
    _static_field_re = re.compile(r'$^') # raw decl pattern; static_fields() normalizes group order
    # --- resource annotations ---
    cache_ann_re = re.compile(r'$^')
    queue_ann_re = re.compile(r'$^')
    config_anchor_re = re.compile(r'$^') # decorators/annotations marking a cross-cutting config fix-site
    # --- module discovery ---
    build_files = ()                     # presence marks a build/module root
    root_marker_files = ()               # presence marks the DEFINITIVE outermost root (stop walking up)

    def return_type_types(self, decl, name, typenames):
        """Repo type(s) named as this function's RETURN type, parsed from its signature `decl`."""
        return set()

    def mutated_types(self, txt, typenames):
        """Repo type(s) whose instance is MUTATED in `txt` (field write on a typed local/param)."""
        return set()

    def static_fields(self, txt):
        """Mutable shared-state field declarations as normalized (name, type) tuples."""
        return []

    def is_config_anchor(self, decorators):
        decs = decorators or []
        return any(self._anchor_one(d) for d in decs)

    def _anchor_one(self, d):
        return bool(self.config_anchor_re.search(d))


# --------------------------------------------------------------------------- Java / Spring
class JavaProfile(LanguageProfile):
    lang = "java"
    exts = (".java",)
    infra_re = re.compile(r'(Constants?|OperateType|TraceLog|Mapper|Service|ServiceI|Controller|Repository|'
                          r'Config|Utils?|Factory|Exception|Test|Application|Aspect|Filter|Builder|'
                          r'Cmd|CmdExe|Qry|Gateway|GatewayImpl|Properties|Enum)$')
    data_like_re = re.compile(r'(DTO|VO|DO|CO|BO|Entity|Model|Request|Response|Event|Form|Bean|Payload|'
                              r'Record|Result|Data|Info|Message|Settings|Config|State|Snapshot)$')
    nondata_re = re.compile(r'(Impl|Transformer|Client|Handler|Manager|Listener|Provider|Resolver|Validator|'
                            r'Interceptor|Aspect|Filter|Job|Task|Runner|Scheduler|Executor|Helper)$')
    logger_re = re.compile(r'(?:Logger|Logger<.*>|^Log|Slf4j)$')
    data_dirs = ("/model/", "/models/", "/dto/", "/entity/", "/entities/", "/vo/", "/bo/", "/co/",
                 "/domain/", "/pojo/", "/payload/", "/event/", "/events/", "/record/")
    svc_dirs = ("/service/", "/controller/", "/handler/", "/config/", "/util", "/transformer/",
                "/client/", "/aspect/", "/filter/", "/provider/")
    new_re = re.compile(r'\bnew\s+([A-Z]\w+)\s*[(<]')
    builder_re = re.compile(r'\b([A-Z]\w+)\.builder\s*\(')
    _static_field_re = re.compile(r'\bstatic\s+(?!final\b)([A-Za-z_][\w.<>\[\]]*)\s+([A-Za-z_]\w*)\s*[=;]')
    cache_ann_re = re.compile(r'\b(DataCache|Cacheable|CachePut|CacheEvict)\b')
    queue_ann_re = re.compile(r'\b(KafkaListener|RabbitListener|RocketMQMessageListener|EventListener|TransactionalEventListener)\b')
    config_anchor_re = re.compile(r'(SpringBootApplication|Configuration|^Enable|EnableAsync|EnableCaching|EnableScheduling)')
    build_files = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    root_marker_files = ("settings.gradle", "settings.gradle.kts")

    def _anchor_one(self, d):
        head = d.split("(")[0]
        return ("SpringBootApplication" in d or head.endswith("Configuration") or head.startswith("Enable"))

    def return_type_types(self, decl, name, typenames):
        # Java/C-family: return type is written BEFORE the method name -> `Fare computeFare(...)`
        m = re.search(r'([A-Za-z_][\w.<>,\[\]\s]*?)\s+' + re.escape(name) + r'\s*\(', decl)
        return {t for t in _CAP.findall(m.group(1)) if t in typenames} if m else set()

    def mutated_types(self, txt, typenames):
        # a var that receives `.setX(...)` and is declared with a repo type (incl. `List<T> v`, param `T v`)
        setters = set(re.findall(r'\b(\w+)\s*\.\s*set[A-Z]\w*\s*\(', txt))
        out = set()
        for var in setters:
            m = re.search(r'\b([A-Z]\w+)\s*(?:<[^>]*>)?\s+' + re.escape(var) + r'\b', txt)
            if m and m.group(1) in typenames:
                out.add(m.group(1))
            m2 = re.search(r'\bList\s*<\s*([A-Z]\w+)\s*>\s+' + re.escape(var) + r'\b', txt)
            if m2 and m2.group(1) in typenames:
                out.add(m2.group(1))
        return out

    def static_fields(self, txt):
        # Java: `static <Type> <name>` -> regex groups are (type, name); normalize to (name, type)
        return [(nm, ty) for ty, nm in self._static_field_re.findall(txt)]


# --------------------------------------------------------------------------- Python
class PythonProfile(LanguageProfile):
    lang = "python"
    exts = (".py", ".pyi")
    # Python rarely uses type-name SUFFIXES; classification leans on package paths + a few real conventions.
    infra_re = re.compile(r'(Service|Manager|Factory|Client|View|ViewSet|Serializer|Middleware|Config|'
                          r'Settings|Exception|Error|Test|Mixin|Admin|Form|Command|Router|Handler|Backend)$')
    data_like_re = re.compile(r'(DTO|Model|Entity|Schema|Request|Response|Event|Payload|Record|Result|'
                              r'Info|Message|State|Snapshot|Data|Params|Config)$')
    nondata_re = re.compile(r'(Service|Manager|Client|Handler|Provider|Resolver|Validator|Middleware|'
                            r'Runner|Scheduler|Executor|Helper|Worker|Task)$')
    logger_re = re.compile(r'(?:Logger|Log)$')
    data_dirs = ("/models", "/schemas", "/entities", "/domain/", "/dto", "/dataclasses", "/types",
                 "/serializers", "/events/", "/dtos")
    svc_dirs = ("/services", "/views", "/api/", "/handlers", "/managers", "/clients", "/middleware",
                "/tasks", "/admin", "/migrations")
    # `Type(...)` construction (CapWords callee). Ambiguous with calls, so callers intersect with typenames.
    new_re = re.compile(r'\b([A-Z]\w+)\s*\(')
    builder_re = re.compile(r'$^')
    # class/module-level annotated mutable state: `name: Type = ...` at class-body indent (not a CONSTANT)
    _static_field_re = re.compile(r'^\s{1,8}([a-z_]\w*)\s*:\s*([A-Za-z_][\w.\[\]]*)\s*=', re.M)
    cache_ann_re = re.compile(r'\b(cache|cached|lru_cache|cached_property|cache_page|cache_result)\b')
    queue_ann_re = re.compile(r'\b(shared_task|task|app\.task|on_event|subscribe|consumer|periodic_task)\b')
    config_anchor_re = re.compile(r'(AppConfig|register|app\.config)')
    build_files = ("pyproject.toml", "setup.py", "setup.cfg")
    root_marker_files = ("pyproject.toml",)

    def is_config_anchor(self, decorators):
        # Python config anchors are usually files (settings.py / apps.py), handled at the candidate layer;
        # decorator-based anchors are rare. Keep the decorator path for @app.route-style registration.
        return super().is_config_anchor(decorators)

    def return_type_types(self, decl, name, typenames):
        # Python annotates the return type AFTER the params: `def name(...) -> Type:`
        m = re.search(r'->\s*([A-Za-z_][\w.\[\],\s\'"]*)\s*:', decl)
        return {t for t in _CAP.findall(m.group(1)) if t in typenames} if m else set()

    def mutated_types(self, txt, typenames):
        # `var.attr = ...` where `var = Type(...)` is constructed in the same text (best-effort, no types)
        constructed = {var: t for var, t in re.findall(r'\b(\w+)\s*=\s*([A-Z]\w+)\s*\(', txt)}
        out = set()
        for var in set(re.findall(r'\b(\w+)\s*\.\s*\w+\s*=', txt)):
            t = constructed.get(var)
            if t in typenames:
                out.add(t)
        return out

    def static_fields(self, txt):
        return list(self._static_field_re.findall(txt))   # already (name, type)


# --------------------------------------------------------------------------- JavaScript / TypeScript
class TypeScriptProfile(LanguageProfile):
    lang = "typescript"
    exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    infra_re = re.compile(r'(Service|Controller|Module|Manager|Factory|Client|Guard|Pipe|Filter|'
                          r'Interceptor|Middleware|Repository|Resolver|Gateway|Config|Exception)$')
    data_like_re = re.compile(r'(DTO|Dto|Model|Entity|Schema|Request|Response|Event|Payload|Record|'
                              r'Result|Info|Message|State|Props|Params|Config|Options)$')
    nondata_re = re.compile(r'(Service|Controller|Manager|Client|Handler|Provider|Resolver|Validator|'
                            r'Interceptor|Guard|Runner|Scheduler|Executor|Helper)$')
    logger_re = re.compile(r'(?:Logger|Log)$')
    data_dirs = ("/models", "/entities", "/dto", "/dtos", "/schemas", "/domain/", "/types", "/interfaces",
                 "/events/")
    svc_dirs = ("/services", "/controllers", "/handlers", "/providers", "/guards", "/middleware",
                "/modules", "/api/", "/resolvers")
    new_re = re.compile(r'\bnew\s+([A-Z]\w+)\s*[(<]')
    builder_re = re.compile(r'$^')
    # `private/readonly foo: Type` field declarations, and TS class properties
    _static_field_re = re.compile(r'\b(?:static|private|public|protected|readonly)\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_][\w<>\[\].]*)')
    cache_ann_re = re.compile(r'\b(Cacheable|CacheKey|CacheTTL|UseInterceptors)\b')
    queue_ann_re = re.compile(r'\b(MessagePattern|EventPattern|OnEvent|Process|Processor|SqsMessageHandler)\b')
    config_anchor_re = re.compile(r'(Module|Global|Injectable\(\s*\{\s*scope)')
    build_files = ("package.json",)
    root_marker_files = ("pnpm-workspace.yaml", "lerna.json", "nx.json")

    def _anchor_one(self, d):
        head = d.split("(")[0]
        return head.endswith("Module") or head == "Global"

    def return_type_types(self, decl, name, typenames):
        # TS annotates return AFTER params: `name(...): Type {`  (JS has no annotation -> empty)
        m = re.search(re.escape(name) + r'\s*\([^)]*\)\s*:\s*([A-Za-z_][\w<>\[\].|\s]*)', decl)
        return {t for t in _CAP.findall(m.group(1)) if t in typenames} if m else set()

    def mutated_types(self, txt, typenames):
        # `this.x = new Type()` / `obj.x =` where obj constructed as `const obj = new Type(`
        constructed = {var: t for var, t in re.findall(r'\b(\w+)\s*=\s*new\s+([A-Z]\w+)', txt)}
        out = set()
        for var in set(re.findall(r'\b(\w+)\s*\.\s*\w+\s*=[^=]', txt)):
            t = constructed.get(var)
            if t in typenames:
                out.add(t)
        return out

    def static_fields(self, txt):
        return list(self._static_field_re.findall(txt))   # already (name, type)


# --------------------------------------------------------------------------- Go
class GoProfile(LanguageProfile):
    lang = "go"
    exts = (".go",)
    infra_re = re.compile(r'(Service|Server|Handler|Repo|Repository|Client|Manager|Factory|Config|Mux|'
                          r'Router|Middleware|Error)$')
    data_like_re = re.compile(r'(Request|Response|Model|Entity|Event|Payload|Record|Result|Info|Message|'
                              r'State|Config|Params|Data|DTO)$')
    nondata_re = re.compile(r'(Service|Server|Handler|Client|Manager|Provider|Resolver|Validator|Runner|'
                            r'Scheduler|Executor|Helper|Worker)$')
    logger_re = re.compile(r'(?:Logger|Log)$')
    data_dirs = ("/models", "/entity", "/entities", "/domain/", "/types", "/dto", "/proto", "/pb/")
    svc_dirs = ("/service", "/services", "/handler", "/handlers", "/server", "/store", "/repository",
                "/internal/", "/api/", "/cmd/")
    # composite literals: `Type{` and `&Type{`, plus `new(Type)`
    new_re = re.compile(r'(?:&|\bnew\s*\(\s*)?\b([A-Z]\w+)\s*[{(]')
    builder_re = re.compile(r'$^')
    # package-level mutable state: `var X Type`
    _static_field_re = re.compile(r'^\s*var\s+([A-Za-z_]\w*)\s+([A-Za-z_][\w.\[\]*]*)', re.M)
    cache_ann_re = re.compile(r'$^')     # Go has no annotations
    queue_ann_re = re.compile(r'$^')
    config_anchor_re = re.compile(r'$^')
    build_files = ("go.mod",)
    root_marker_files = ("go.work",)

    def is_config_anchor(self, decorators):
        return False                     # Go has no decorators; config anchors handled by file convention

    def return_type_types(self, decl, name, typenames):
        # Go: return type(s) AFTER the params -> `func name(...) (T, error)` / `func (r R) name(...) T`
        m = re.search(r'\bfunc\s+(?:\([^)]*\)\s*)?' + re.escape(name) + r'\s*\([^)]*\)\s*\(?([^){]*)', decl)
        return {t for t in _CAP.findall(m.group(1)) if t in typenames} if m else set()

    def mutated_types(self, txt, typenames):
        constructed = {var: t for var, t in re.findall(r'\b(\w+)\s*:?=\s*&?\s*([A-Z]\w+)\s*\{', txt)}
        out = set()
        for var in set(re.findall(r'\b(\w+)\s*\.\s*\w+\s*=[^=]', txt)):
            t = constructed.get(var)
            if t in typenames:
                out.add(t)
        return out

    def static_fields(self, txt):
        return list(self._static_field_re.findall(txt))   # already (name, type)


_PROFILES = [JavaProfile(), PythonProfile(), TypeScriptProfile(), GoProfile()]
_BY_EXT = {e: p for p in _PROFILES for e in p.exts}
_DEFAULT = JavaProfile()


def profile_for(path):
    """The profile for a single file, by extension."""
    return _BY_EXT.get(os.path.splitext(path)[1].lower(), _DEFAULT)


def dominant_profile(rg):
    """The profile for the language that owns the most files in the repo — for repo-level decisions
    (state-graph construction, module discovery) that need ONE profile."""
    import collections
    counts = collections.Counter()
    for n in rg.nodes.values():
        ext = os.path.splitext(getattr(n, "file", "") or "")[1].lower()
        if ext in _BY_EXT:
            counts[_BY_EXT[ext].lang] += 1
    if not counts:
        return _DEFAULT
    top = counts.most_common(1)[0][0]
    return next(p for p in _PROFILES if p.lang == top)


def dominant_profile_for_dir(repo_dir):
    """Repo-level profile by walking the source tree (used before a graph exists, e.g. module discovery)."""
    import collections
    counts = collections.Counter()
    for dp, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "target", "build", "node_modules", ".venv", ".vard", ".idea", "dist")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in _BY_EXT:
                counts[_BY_EXT[ext].lang] += 1
    if not counts:
        return _DEFAULT
    top = counts.most_common(1)[0][0]
    return next(p for p in _PROFILES if p.lang == top)
