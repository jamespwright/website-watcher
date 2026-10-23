"""Fallback domain: passes fields through untouched."""
from watcher.core.domain import Domain, register_domain


@register_domain
class Generic(Domain):
    name = "generic"
