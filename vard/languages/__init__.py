"""Language providers — the extension point. Register a provider per language;
everything above (graph, resources, coupling, freshness, MCP) is language-agnostic.

To add a language: add it to treesitter_provider.LANGS (node-type config), or drop in
a new provider module and register it below.
"""
import os
from .base import Symbol, CallSite, FileArtifact, LanguageProvider  # noqa
from .treesitter_provider import TreeSitterProvider, LANGS

_PROVIDERS = []
_BY_EXT = {}


def register(provider):
    _PROVIDERS.append(provider)
    for e in provider.extensions:
        _BY_EXT[e] = provider


# One tree-sitter provider instance per configured language (python/java/js/ts/tsx/go).
for _lang in LANGS:
    register(TreeSitterProvider(_lang))


def provider_for(path):
    return _BY_EXT.get(os.path.splitext(path)[1].lower())


def supported_extensions():
    return set(_BY_EXT.keys())
