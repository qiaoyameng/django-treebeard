Synchronizing trees with an external manifest
=============================================

.. module:: treebeard.sync

When an upstream system delivers a full directory tree every day and every
node carries a stable external identity, recreating the subtree with
:py:meth:`~treebeard.mp_tree.MP_NodeManager.load_bulk` would throw away all
primary keys. :func:`sync_tree` instead reconciles the manifest with the
current database content and applies only the differences, using the regular
:py:class:`~treebeard.mp_tree.MP_Node` add/move/delete primitives:

* nodes present in both places get their fields updated (a rename keeps the
  primary key, even when the node is moved in the same sync),
* nodes whose position changed are moved,
* new identities are added,
* nodes missing from the manifest are either kept (default) or deleted,
  depending on ``on_missing``. Before deleting, the sync checks that no
  manifest-referenced descendants remain below the node and consults the
  optional ``protect`` business predicate.

Only the scope given by ``parent`` is modified; a manifest identity that
matches a node outside the scope raises
:py:class:`~treebeard.exceptions.IdentityConflict`. The manifest is fully
validated before anything is written: duplicate identities, cycles, unknown
parent references, invalid ordering and structures that do not fit the
available path space are all rejected without touching the database.

A dry-run returns the planned changes together with a digest of the current
structure; passing that digest as ``expected_summary`` to the apply call
guards against concurrent modifications. Applying runs in a single
transaction, and applying the same manifest twice produces no changes.

Example:

.. code-block:: python

   from treebeard.sync import sync_tree

   manifest = [
       {"external_id": "a", "data": {"desc": "A"}, "children": [
           {"external_id": "b", "data": {"desc": "B"}},
       ]},
   ]

   # see what would happen
   plan = sync_tree(MyNode, manifest, parent=section, dry_run=True)
   print(plan)

   # apply, making sure the tree did not change in the meantime
   sync_tree(MyNode, manifest, parent=section, expected_summary=plan.old_summary)

The same entry point is available on the manager as
``MyNode.objects.sync(manifest, parent=section, ...)``.

.. autofunction:: sync_tree

.. autoclass:: SyncPlan
   :members:

.. autoclass:: SyncChange
