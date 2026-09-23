"""
Depot root resolution for git-p4son.

Turns the depot root stored in .git-p4son/config.toml into the path Perforce
commands run against, querying the client spec along the way.
"""

from dataclasses import dataclass

from .config import WORKSPACE_PLACEHOLDER, expand_depot_root, get_depot_root
from .log import log
from .perforce import P4ClientSpec, get_client_spec


@dataclass
class ResolvedDepot:
    """The configured depot root with any placeholder expanded, plus the
    client spec it was resolved against."""
    depot_root: str
    client_spec: P4ClientSpec | None


def resolve_depot_root(workspace_dir: str) -> ResolvedDepot | None:
    """Resolve the configured depot root, or None (with an error logged).

    The client spec is queried once here: its name resolves a $(workspace)
    placeholder in the depot root, and its line-ending/clobber options feed
    the writable-file handling in sync.
    """
    log.heading('Finding depot root')
    depot_root = get_depot_root(workspace_dir)
    if not depot_root:
        log.error('No depot root configured. Run "git p4son init" first.')
        return None

    client_spec = get_client_spec(workspace_dir)
    if WORKSPACE_PLACEHOLDER in depot_root and not client_spec:
        log.error('Cannot resolve $(workspace) in depot root: not inside a '
                  'Perforce workspace')
        return None
    if client_spec:
        depot_root = expand_depot_root(depot_root, client_spec.name)
    log.success(depot_root)
    return ResolvedDepot(depot_root=depot_root, client_spec=client_spec)
