#!/usr/bin/env python3
"""
VARD — Data-Resource layer (language-agnostic, ruleset-driven).

Consumes uniform CallSites + decorators from the language providers (see languages/)
and classifies reads/writes to caches / DBs / queues, building implicit-coupling edges
(writer -> resource <- reader). Vocabulary is a ruleset (discover.py infers it per repo);
DEFAULT_RULESET covers common Python/Java-Spring/Node stacks out of the box.
"""
import re
from collections import defaultdict

DEFAULT_RULESET = {
    # cache
    "cache_receivers": ["redis_connection", "redis", "cache", "_redis", "rd", "conn", "redis_cli", "client",
                        "caches", "redistemplate", "stringredistemplate", "jedis", "redisson", "cachemanager",
                        "opsforvalue", "opsforhash", "memcache", "mc"],
    "cache_read":  ["get", "hget", "hgetall", "hmget", "smembers", "lrange", "exists", "keys", "scan_iter",
                    "zrange", "zscore", "mget", "getex", "hkeys", "sismember", "get_many", "getifpresent"],
    "cache_write": ["set", "hset", "hmset", "lpush", "rpush", "zadd", "incr", "decr", "setex", "expire",
                    "delete", "del", "sadd", "hdel", "setnx", "mset", "rename", "set_many", "put", "evict"],
    # queue
    "queue_enqueue_attrs": ["delay", "apply_async", "apply", "send_task", "enqueue_at", "enqueue_in", "send",
                            "publish", "convertandsend", "sendmessage", "emit", "dispatch", "produce", "offer"],
    "queue_enqueue_funcs": ["enqueue", "send_task", "publish"],
    "queue_decorators": ["task", "job", "shared_task", "periodic_task", "celery_task", "kafkalistener",
                         "rabbitlistener", "jmslistener", "streamlistener", "eventlistener", "messagemapping",
                         "scheduled", "sqslistener"],
    # db
    "db_read_attrs": ["query", "objects", "find", "findone", "findbyid", "findall", "get", "get_or_404",
                      "select", "fetch", "filter", "all", "first", "findoneby", "getbyid"],
    "db_write_attrs": ["save", "saveall", "insert", "update", "delete", "deletebyid", "create", "persist",
                       "bulk_create", "remove", "upsert", "createmany", "updatemany"],
    "db_model_base_markers": ["Model", "Base", "Document", "Entity", "BaseModel"],
    # cache decorators (Spring @Cacheable etc.)
    "cache_decorators": ["cacheable", "cacheput", "cacheevict"],
}


def _norm_key(arg0):
    if not arg0:
        return None
    s = re.sub(r"\s+", "", arg0).strip("\"'`")
    s = re.sub(r"\$\{[^}]*\}", "{}", s)        # template placeholders
    return s[:80] or None


def _first_str(text):
    m = re.search(r"""["'`]([^"'`]+)["'`]""", text or "")
    return m.group(1) if m else None


def _last_id(receiver):
    if not receiver:
        return ""
    return re.split(r"[.(]", receiver)[0] if "." not in receiver else receiver.split(".")[-1].split("(")[0]


# A receiver that is genuinely a data handle (vs. an array/jQuery/etc.)
DATA_HANDLE = re.compile(r"(repository|repositories|repo|model|models|dao|mapper|entity|collection|store|table|session|datasource)$", re.I)
# Unambiguous DB methods (won't collide with arrays / jQuery)
STRONG_DB_READ = {"findbyid", "findone", "findall", "findoneby", "getbyid", "query", "objects",
                  "get_or_404", "findoneorfail", "findmany"}
STRONG_DB_WRITE = {"save", "saveall", "persist", "deletebyid", "upsert", "bulk_create",
                   "createmany", "updatemany", "insertone", "updateone", "deleteone", "insertmany"}


class ResourceExtractor:
    def __init__(self, rg, ruleset=None):
        self.rg = rg
        self.rs = {**DEFAULT_RULESET, **(ruleset or {})}
        self.res_nodes, self.edges = {}, []
        self.writers, self.readers = defaultdict(set), defaultdict(set)

    def _add(self, fn_id, rtype, key, kind):
        if not key or not fn_id or fn_id not in self.rg.nodes:
            return
        rid = f"{rtype}:{key}"
        self.res_nodes.setdefault(rid, {"type": rtype, "key": key})
        self.edges.append((fn_id, rid, kind))
        (self.writers if kind in ("writes", "enqueues") else self.readers)[rid].add(fn_id)

    def run(self):
        rs = self.rs
        crecv, cread, cwrite = set(rs["cache_receivers"]), set(rs["cache_read"]), set(rs["cache_write"])
        qattr, qfunc = set(rs["queue_enqueue_attrs"]), set(rs["queue_enqueue_funcs"])
        dread, dwrite = set(rs["db_read_attrs"]), set(rs["db_write_attrs"])
        qdec, cdec = set(rs["queue_decorators"]), set(rs["cache_decorators"])

        for cs in getattr(self.rg, "call_sites", []):
            fid = f"{cs.file}::{cs.enclosing_qual}"
            if fid not in self.rg.nodes:
                fid = f"{cs.file}::<module>"
            m = (cs.method or "").lower()
            recv = (cs.receiver or "")
            rl = recv.lower()
            base = _last_id(rl)
            is_cache_recv = base in crecv or "redis" in rl or "cache" in rl or "memcache" in rl
            # CACHE (checked first; 'set'/'get' are cache, not db)
            if m in cwrite and is_cache_recv:
                self._add(fid, "cache", _norm_key(cs.arg0), "writes")
            elif m in cread and is_cache_recv:
                self._add(fid, "cache", _norm_key(cs.arg0), "reads")
            # QUEUE producer
            elif m in qattr:
                topic = _first_str(cs.arg0) or _last_id(recv) or _norm_key(cs.arg0)
                self._add(fid, "queue", (topic or "").lower(), "enqueues")
            elif not recv and m in qfunc:
                self._add(fid, "queue", (_first_str(cs.arg0) or _norm_key(cs.arg0) or "").lower(), "enqueues")
            # DB — resolve the receiver to its DECLARED TYPE when known (repository ->
            # AccountRepository). Only count it as DB if the method is unambiguous OR the
            # handle is genuinely a data handle (drops $.get / array.find / jQuery noise).
            elif (m in dwrite or m in dread) and base and base not in ("self", "this", ""):
                vt = getattr(self.rg, "var_types", {}).get(cs.file, {})
                ident = _last_id(recv)
                typ = vt.get(ident, ident)
                strong = m in STRONG_DB_READ or m in STRONG_DB_WRITE
                if strong or DATA_HANDLE.search(typ or "") or DATA_HANDLE.search(ident or ""):
                    is_write = m in dwrite or m in STRONG_DB_WRITE
                    self._add(fid, "table", (typ or ident).lower(), "writes" if is_write else "reads")

        # decorators: queue consumers + cache annotations
        for nid, decs in getattr(self.rg, "node_decorators", {}).items():
            n = self.rg.nodes.get(nid)
            for d in decs:
                dn = d.split("(")[0].split(".")[-1].lower()
                if dn in qdec:
                    topic = _first_str(d) or (n.name if n else dn)
                    self._add(nid, "queue", str(topic).lower(), "consumes")
                elif dn in cdec:
                    key = _first_str(d) or (n.name if n else dn)
                    kind = "reads" if "evict" not in dn else "writes"
                    self._add(nid, "cache", str(key).lower(), kind)

        for rid in self.res_nodes:
            self.rg.G.add_node(rid, type="resource")
        for s, r, k in self.edges:
            self.rg.G.add_edge(s, r, key=k)
        return self

    def stats(self):
        from collections import Counter
        return {"resource_nodes": dict(Counter(v["type"] for v in self.res_nodes.values())),
                "n_resources": len(self.res_nodes),
                "edges": dict(Counter(k for _, _, k in self.edges)), "n_edges": len(self.edges)}

    def coupling_pairs(self):
        out = []
        for rid in self.res_nodes:
            for w in self.writers.get(rid, set()):
                for r in self.readers.get(rid, set()):
                    if w != r and not w.endswith("<module>") and not r.endswith("<module>"):
                        out.append((w, r, rid))
        return out


def extract(rg, ruleset=None):
    return ResourceExtractor(rg, ruleset).run()
