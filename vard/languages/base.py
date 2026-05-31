"""Uniform, language-agnostic representation produced by every provider."""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Symbol:
    qual: str                 # qualified name, e.g. com.x.OrderService.cache
    type: str                 # module | class | function | method
    file: str
    start: int                # 1-based line
    end: int
    name: str
    parent_qual: Optional[str] = None
    bases: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)   # e.g. KafkaListener("orders")


@dataclass
class CallSite:
    enclosing_qual: str       # which symbol contains this call (or module)
    receiver: str             # text before the method, e.g. "redis.opsForValue()"
    method: str               # e.g. "set"
    arg0: Optional[str]       # textual first argument (key, when present)
    line: int
    file: str


@dataclass
class FileArtifact:
    file: str
    lang: str
    symbols: List[Symbol] = field(default_factory=list)
    calls: List[CallSite] = field(default_factory=list)
    imports: List[Tuple[str, str]] = field(default_factory=list)   # (alias, target)
    var_types: dict = field(default_factory=dict)                  # var/field name -> declared type


class LanguageProvider:
    """Implement .extensions and .parse(repo, rel, src) -> FileArtifact."""
    extensions: set = set()
    lang: str = ""

    def parse(self, repo_dir: str, rel: str, src: str) -> FileArtifact:
        raise NotImplementedError
