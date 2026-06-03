"""Per-tool denylist configuration. See SPEC §10.

Denylists are *configuration*, not code: each profile names a set of JSON
keys the `Prune` stage will strip at any depth. Three profiles ship out of
the box (k8s, postgres, azure) plus a `passthrough` profile that strips
nothing. Adding a new profile is a one-line entry in `PROFILES`.
"""

from __future__ import annotations

# -- k8s (default for kube-doctor) -------------------------------------------
# Bookkeeping fields that bloat `kubectl get … -o json` with no diagnostic
# value. `lastTransitionTime` lives inside `status.conditions[*]` — the prune
# stage matches by key name at any depth, so the bare key suffices.
K8S_DENYLIST: set[str] = {
    "managedFields",
    "resourceVersion",
    "uid",
    "generation",
    "creationTimestamp",
    "selfLink",
    "ownerReferences",
    "finalizers",
    "annotations",
    "labels",
    "lastTransitionTime",
}

# -- postgres (default for pg-doctor) ----------------------------------------
# Drop per-backend bookkeeping from pg_stat_* dumps; keep rate/aggregate
# fields that actually tell you what's going on.
POSTGRES_DENYLIST: set[str] = {
    "pid",
    "backend_start",
    "query_start",
    "xact_start",
    "state_change",
    "application_name",
    "client_addr",
    "client_hostname",
    "client_port",
    "backend_xid",
    "backend_xmin",
    "leader_pid",
}

# -- azure (default for Azure MCP server) ------------------------------------
# Strip ARM envelope cruft: provisioning history, etags, system metadata, and
# the GUID-shaped `id` field (resources also carry a `name` you can rely on).
AZURE_DENYLIST: set[str] = {
    "systemData",
    "etag",
    "provisioningState",
    "id",
    "managedBy",
    "kind",
}

PROFILES: dict[str, set[str]] = {
    "passthrough": set(),
    "k8s": K8S_DENYLIST,
    "postgres": POSTGRES_DENYLIST,
    "azure": AZURE_DENYLIST,
}


def get_profile(name: str) -> set[str]:
    """Look up a denylist profile by name. Unknown names → passthrough."""
    return PROFILES.get(name, PROFILES["passthrough"])
