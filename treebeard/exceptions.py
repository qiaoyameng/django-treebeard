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


class SyncError(Exception):
    """Base class for errors raised while synchronizing a tree from a manifest."""


class InvalidSyncData(SyncError):
    """Raised when a synchronization manifest is malformed or uses unsupported fields."""


class CrossScopeIdentityConflict(SyncError):
    """Raised when a manifest identity exists outside the synchronized subtree."""


class SyncCycleError(SyncError):
    """Raised when synchronizing would move a node beneath one of its descendants."""


class InvalidSyncOrdering(SyncError):
    """Raised when the manifest order conflicts with a model's ``node_order_by``."""


class SyncSummaryMismatch(SyncError):
    """Raised when applying a plan whose old-tree summary no longer matches."""


class ProtectedNodeError(SyncError):
    """Raised when a business protection callback refuses node deletion."""


class PathOverflow(Exception):
    """
    Raised when trying to add or move a node to a position where no more nodes
    can be added (see :attr:`~treebeard.MP_Node.path` and
    :attr:`~treebeard.MP_Node.alphabet` for more info)
    """
