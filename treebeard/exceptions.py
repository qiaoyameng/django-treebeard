"""Treebeard exceptions"""


class InvalidPosition(Exception):
    """Raised when passing an invalid pos value"""


class InvalidMoveToDescendant(Exception):
    """Raised when attempting to move a node to one of it's descendants."""


class NodeAlreadySaved(Exception):
    """
    Raised when attempting to add a node which is already saved to the
    database.
    """


class MissingNodeOrderBy(Exception):
    """
    Raised when an operation needs a missing
    :attr:`~treebeard.MP_Node.node_order_by` attribute
    """


class PathOverflow(Exception):
    """
    Raised when trying to add or move a node to a position where no more nodes
    can be added (see :attr:`~treebeard.MP_Node.path` and
    :attr:`~treebeard.MP_Node.alphabet` for more info)
    """


class SyncError(Exception):
    """
    Base class for errors raised while synchronizing a tree with an
    external manifest (see :func:`treebeard.sync.sync_tree`).
    """


class DuplicateIdentity(SyncError):
    """
    Raised when an external identity appears more than once, either in the
    manifest or in the existing database content of the synchronized scope.
    """


class ManifestCycle(SyncError):
    """Raised when the manifest parent references form a cycle."""


class InvalidManifestOrder(SyncError):
    """
    Raised when the manifest ordering information is invalid (e.g. duplicate
    or non-contiguous positions, or an order that contradicts
    :attr:`~treebeard.MP_Node.node_order_by`).
    """


class IdentityConflict(SyncError):
    """
    Raised when a manifest identity matches a node that lives outside the
    synchronized scope. Synchronization never touches nodes outside its
    scope, so such a clash cannot be resolved and is reported instead.
    """


class ProtectedNode(SyncError):
    """
    Raised when a missing node would be deleted but is protected by the
    business ``protect`` predicate, or when a node scheduled for deletion
    still has descendants referenced by the manifest.
    """


class StaleSyncSummary(SyncError):
    """
    Raised when the structure summary recorded by a dry-run no longer
    matches the database at apply time, meaning the tree changed in
    between and the planned changes may not apply cleanly.
    """
