"""Materialized Path Trees"""

import collections
import warnings
from functools import cache
from typing import Any, NamedTuple

from django.core import serializers
from django.db import connections, models, router, transaction
from django.db.models import F, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import Concat, Greatest, Length, Substr
from django.dispatch import Signal
from django.utils.translation import gettext_noop as _

from treebeard.deprecation import RemovedInTreebeard8Warning
from treebeard.exceptions import InvalidMoveToDescendant, InvalidPosition, PathOverflow
from treebeard.models import Node, NodeManager
from treebeard.numconv import NumConv
from treebeard.utils import check_create_args, prepare_dumpdata_for_loading, save_m2m

path_updated = Signal()
nodes_deleted = Signal()


class BulkMoveResult(NamedTuple):
    """
    Describes where a single node ended up after a :meth:`MP_NodeManager.move_bulk`
    operation. Results are returned in the same order as the requested moves.

    Attributes:
        node_pk: Primary key of the moved node.
        parent_pk: Primary key of its final parent, or ``None`` for a root node.
        new_path: Final materialized path of the node.
        depth: Final depth of the node.
        sibling_index: 1-indexed position amongst its final siblings.
        moved: ``False`` when the request was a no-op (the node was already in
            the requested place); ``True`` otherwise.
    """

    node_pk: Any
    parent_pk: Any
    new_path: str
    depth: int
    sibling_index: int
    moved: bool


class MP_NodeQuerySet(models.query.QuerySet):
    """
    Custom queryset for the tree node manager.

    Needed only for the custom delete method.
    """

    def delete(self, *args, **kwargs):
        """
        Custom delete method, will remove all descendant nodes to ensure a
        consistent tree (no orphans)

        :returns: tuple of the number of objects deleted and a dictionary
                  with the number of deletions per object type
        """
        # we'll have to manually run through all the nodes that are going
        # to be deleted and remove nodes from the list if an ancestor is
        # already getting removed, since that would be redundant
        removed = {}
        for node in self.order_by("depth", "path").only("path", "depth", "numchild").prefetch_related(None).iterator():
            found = False
            for depth in range(1, int(len(node.path) / node.steplen)):
                path = node._get_basepath(node.path, depth)
                if path in removed:
                    # we are already removing an ancestor of this node, so skip it
                    found = True
                    break
            if not found:
                removed[node.path] = node

        # ok, got the minimal list of nodes to remove...
        # we must also remove their children
        # and update every parent node's numchild attribute
        parents = collections.Counter()  # Mapping of parent path to the number of children it has lost
        pks_to_remove = []
        paths_to_remove = []
        for path, node in removed.items():
            if parentpath := node._get_basepath(node.path, node.depth - 1):
                parents[parentpath] += 1

            if node.is_leaf():
                pks_to_remove.append(node.pk)  # More efficient than querying by path
            else:
                paths_to_remove.append(node.path)

        model = self.model.objects.tree_model

        # Save the updated numchild of all parents
        for path, num_lost in parents.items():
            model.objects.filter(path=path).update(numchild=Greatest(F("numchild") - num_lost, 0))

        # Django will handle this as a SELECT and then a DELETE of
        # ids, and will deal with removing related objects
        query = Q(pk__in=pks_to_remove)
        for path in paths_to_remove:
            query |= Q(path__startswith=path)
        count, deletion_map = super(MP_NodeQuerySet, model.objects.filter(query)).delete(*args, **kwargs)
        if count > 0:
            nodes_deleted.send(
                sender=model, pks_to_remove=pks_to_remove, paths_to_remove=paths_to_remove, using=self.db
            )
        return count, deletion_map

    delete.alters_data = True
    delete.queryset_only = True


class MP_NodeManager(NodeManager):
    """Custom manager for nodes in a Materialized Path tree."""

    def get_queryset(self):
        """Sets the custom queryset as the default."""
        return MP_NodeQuerySet(self.model, using=self._db).order_by("path")

    def get_tree(self, parent=None, max_depth: int | None = None):
        """
        :returns:

            A queryset of nodes ordered as DFS, including the parent. If
            no parent is given, the entire tree is returned.

            If max_depth is set then the tree is limited to the specified depth, relative
            to the parent (or the root if no parent is specified).
        """
        cls = self.tree_model

        if parent and parent.is_leaf():
            return cls.objects.filter(pk=parent.pk)

        filters = {}

        if parent:
            filters["path__startswith"] = parent.path
            filters["depth__gte"] = parent.depth

        if max_depth is not None:
            filters["depth__lte"] = max_depth + (parent.depth if parent else 0)

        return cls.objects.filter(**filters)

    def fix_tree(self, fix_paths=False, parent=None):
        """
        Solves some problems that can appear when transactions are not used and
        a piece of code breaks, leaving the tree in an inconsistent state.

        The problems this method solves are:

           1. Nodes with an incorrect ``depth`` or ``numchild`` values due to
              incorrect code and lack of database transactions.
           2. "Holes" in the tree. This is normal if you move/delete nodes a
              lot. Holes in a tree don't affect performance,
           3. Incorrect ordering of nodes when ``node_order_by`` is enabled.
              Ordering is enforced on *node insertion*, so if an attribute in
              ``node_order_by`` is modified after the node is inserted, the
              tree ordering will be inconsistent.

        :param fix_paths:

            A boolean value. If True, a slower, more complex fix_tree method
            will be attempted. If False (the default), it will use a safe (and
            fast!) fix approach, but it will only solve the ``depth`` and
            ``numchild`` nodes, it won't fix the tree holes or broken path
            ordering.

        :param parent:

            If provided, limits the operation to descendants of the given node.
            If not provided, the entire tree will be fixed.

            Fixing only part of a tree will only work if the parent itself is valid.
        """
        cls = self.tree_model

        qs = cls.objects.filter(path__startswith=parent.path) if parent else cls.objects.all()

        # fix the depth field; we need the exclude query to speed up postgres
        qs.exclude(depth=Length("path") / cls.steplen).update(depth=Length("path") / cls.steplen)

        # fix the numchild field
        self._fix_numchild(qs)

        if fix_paths:
            with transaction.atomic():
                # To fix holes and mis-orderings in paths, we consider each non-leaf node in turn
                # and ensure that its children's path values are consecutive (and in the order
                # given by node_order_by, if applicable). children_to_fix is a queue of child sets
                # that we know about but have not yet fixed, expressed as a tuple of
                # (parent_path, depth). Since we're updating paths as we go, we must take care to
                # only add items to this list after the corresponding parent node has been fixed
                # (and is thus not going to change).

                # Initially children_to_fix is the set of root nodes, i.e. ones with a path
                # starting with '' and depth 1.
                children_to_fix = [(parent.path, parent.depth + 1)] if parent else [("", 1)]

                while children_to_fix:
                    parent_path, depth = children_to_fix.pop(0)

                    children = cls.objects.filter(path__startswith=parent_path, depth=depth).values(
                        "pk", "path", "depth", "numchild"
                    )

                    desired_sequence = children.order_by(*(cls.node_order_by or ["path"]))

                    # mapping of current path position (converted to numeric) to item
                    actual_sequence = {}

                    # highest numeric path position currently in use
                    max_position = None

                    # loop over items to populate actual_sequence and max_position
                    for item in desired_sequence:
                        actual_position = cls._str2int(item["path"][-cls.steplen :])
                        actual_sequence[actual_position] = item
                        if max_position is None or actual_position > max_position:
                            max_position = actual_position

                    # loop over items to perform path adjustments
                    for i, item in enumerate(desired_sequence):
                        desired_position = i + 1  # positions are 1-indexed
                        actual_position = cls._str2int(item["path"][-cls.steplen :])
                        if actual_position == desired_position:
                            pass
                        else:
                            # if a node is already in the desired position, move that node
                            # to max_position + 1 to get it out of the way
                            occupant = actual_sequence.get(desired_position)
                            if occupant:
                                old_path = occupant["path"]
                                max_position += 1
                                new_path = cls._get_path(parent_path, depth, max_position)
                                if len(new_path) > len(old_path):
                                    previous_max_path = cls._get_path(parent_path, depth, max_position - 1)
                                    raise PathOverflow(_(f"Path Overflow from: '{previous_max_path}'"))

                                self._rewrite_node_path(old_path, new_path)
                                # update actual_sequence to reflect the new position
                                actual_sequence[max_position] = occupant
                                del actual_sequence[desired_position]
                                occupant["path"] = new_path

                            # move item into the (now vacated) desired position
                            old_path = item["path"]
                            new_path = cls._get_path(parent_path, depth, desired_position)
                            self._rewrite_node_path(old_path, new_path)
                            # update actual_sequence to reflect the new position
                            actual_sequence[desired_position] = item
                            del actual_sequence[actual_position]
                            item["path"] = new_path

                        if item["numchild"]:
                            # this item has children to process, and we have now moved the parent
                            # node into its final position, so it's safe to add to children_to_fix
                            children_to_fix.append((item["path"], depth + 1))

    def _rewrite_node_path(self, old_path, new_path):
        queryset = self.tree_model.objects.filter(path__startswith=old_path)
        update_count = queryset.update(path=Concat(Value(new_path), Substr("path", len(old_path) + 1)))
        if update_count > 0:
            path_updated.send(sender=self.tree_model, old_path=old_path, new_path=new_path, using=queryset.db)

    def _fix_numchild(self, qs):
        vendor = connections[router.db_for_write(self.tree_model)].vendor
        child_subquery = (
            self.tree_model.objects.alias(path_length=Length("path"))
            .order_by()
            .filter(path__startswith=OuterRef("path"), path_length=Length(OuterRef("path")) + self.model.steplen)
            .annotate(count=Func(F("pk"), function="Count"))
            .values("count")
        )
        qs = qs.annotate(real_numchild=Subquery(child_subquery, output_field=models.IntegerField())).exclude(
            numchild=F("real_numchild")
        )

        if vendor != "mysql":
            qs.update(numchild=F("real_numchild"))
        else:  # pragma: no cover
            # Our friend MySQL doesn't support update queries that use a select from the same table
            # So we have to update each object individually
            to_update = []
            for node in qs.iterator():
                node.numchild = node.real_numchild
                to_update.append(node)

            self.tree_model.objects.bulk_update(to_update, ["numchild"])

    def find_problems(self, parent=None):
        """
        Checks for problems in the tree structure, problems can occur when:

           1. your code breaks and you get incomplete transactions (always
              use transactions!)
           2. changing the ``steplen`` value in a model (you must
              :meth:`dump_bulk` first, change ``steplen`` and then
              :meth:`load_bulk`

        :param parent:

            If provided, limits the check to the descendants of this node.
            If not provided, the entire tree will be checked.

        :returns: A tuple of five lists:

                  1. a list of ids of nodes with characters not found in the
                     ``alphabet``
                  2. a list of ids of nodes when a wrong ``path`` length
                     according to ``steplen``
                  3. a list of ids of orphaned nodes
                  4. a list of ids of nodes with the wrong depth value for
                     their path
                  5. a list of ids nodes that report a wrong number of children
        """
        cls = self.tree_model

        if parent is not None:
            qs = cls.objects.filter(path__startswith=parent.path)
        else:
            qs = cls.objects.all()

        evil_chars, bad_steplen, orphans = [], [], []
        wrong_depth, wrong_numchild = [], []
        for node in qs.iterator():
            found_error = False
            for char in node.path:
                if char not in cls.alphabet:
                    evil_chars.append(node.pk)
                    found_error = True
                    break
            if found_error:
                continue
            if len(node.path) % cls.steplen:
                bad_steplen.append(node.pk)
                continue
            try:
                self.get_parent(node, update=True)
            except cls.DoesNotExist:
                orphans.append(node.pk)
                continue

            if node.depth != int(len(node.path) / cls.steplen):
                wrong_depth.append(node.pk)
                continue

            real_numchild = (
                cls.objects.alias(computed_depth=Length("path") / cls.steplen)
                .filter(path__range=cls._get_children_path_interval(node.path), computed_depth=node.depth + 1)
                .count()
            )
            if real_numchild != node.numchild:
                wrong_numchild.append(node.pk)
                continue

        return evil_chars, bad_steplen, orphans, wrong_depth, wrong_numchild

    @transaction.atomic
    @check_create_args
    def add_root(self, create_kwargs=None, *, instance=None):
        """
        Adds a root node to the tree.

        This method saves the node in database. The object is populated as if via:

        ```
        obj = self.model(**create_kwargs)
        ```

        if create_kwargs are supplied. If an unsaved model `instance` is supplied, it is used directly.

        :raise PathOverflow: when no more root objects can be added
        """
        # do we have a root node already?
        last_root = self.get_last_root_node()

        if last_root and last_root.node_order_by:
            # There are root nodes and node_order_by has been set.
            # Delegate sorted insertion to add_sibling.
            # We must pass an instance here to ensure that the right object is created for
            # models with multi-table inheritance.
            return self.add_sibling(
                last_root,
                "sorted-sibling",
                instance=instance or self.model(**create_kwargs),
            )

        if last_root:
            # adding the new root node as the last one
            newpath = last_root._inc_path()
        else:
            # adding the first root node
            newpath = self.model._get_path(None, 1, 1)

        newobj = instance or self.model(**create_kwargs)
        newobj.depth = 1
        newobj.path = newpath
        # saving the instance before returning it
        newobj.save(using=self._db)
        return newobj

    @transaction.atomic
    @check_create_args
    def add_child(self, target, create_kwargs=None, *, instance=None):
        """
        Adds a child to the node.

        This method saves the node in database. The object is populated as if via:

        ```
        obj = self.__class__(**create_kwargs)
        ```

        if `create_kwargs` are supplied. If an unsaved model `instance` is supplied, it is used directly.

        :raise PathOverflow: when no more child nodes can be added
        """
        # Lock the parent row
        node = target.__class__.objects.select_for_update().get(pk=target.pk)
        if self.model.node_order_by and not node.is_leaf():
            # there are child nodes and node_order_by has been set
            # delegate sorted insertion to add_sibling
            target.numchild += 1
            return self.add_sibling(self.get_last_child(node), "sorted-sibling", create_kwargs, instance=instance)

        newobj = instance or self.model(**create_kwargs)
        newobj.depth = node.depth + 1

        if node.is_leaf():
            # the node had no children, adding the first child
            newobj.path = self.model._get_path(node.path, newobj.depth, 1)
            max_length = self.model._meta.get_field("path").max_length
            if len(newobj.path) > max_length:
                raise PathOverflow(
                    _(
                        "The new node is too deep in the tree, try"
                        " increasing the path.max_length property"
                        " and UPDATE your database"
                    )
                )
        else:
            # adding the new child as the last one
            newobj.path = self.get_last_child(node)._inc_path()

        # Increment numchild on the parent, and also update the object in memory in case the caller reuses it
        self.tree_model.objects.filter(pk=node.pk).update(numchild=F("numchild") + 1)
        target.numchild = node.numchild + 1

        # saving the instance before returning it
        newobj._cached_parent_obj = target
        newobj.save(using=self._db)

        return newobj

    @transaction.atomic
    @check_create_args
    def add_sibling(self, target, pos=None, create_kwargs=None, *, instance=None):
        """
        Adds a new node as a sibling to the current node object.

        This method saves the node in database. The object is populated as if via:

        ```
        obj = self.__class__(**create_kwargs)
        ```

        if `create_kwargs` are supplied. If an unsaved model `instance` is supplied, it is used directly.

        :raise PathOverflow: when the library can't make room for the
           node's new position
        """
        pos = self._prepare_pos_var_for_add_sibling(pos)

        newobj = instance or self.model(**create_kwargs)
        newobj.depth = target.depth

        if pos == "sorted-sibling":
            siblings = self.get_sorted_pos_queryset(self.get_siblings(target), newobj)
            first = siblings.first()
            newpos = first._get_lastpos_in_path() if first else None
            if newpos is None:
                pos = "last-sibling"
        else:
            newpos, siblings = None, []

        _, newpath = self._reorder_nodes_before_add_or_move(pos, newpos, target.depth, target, siblings, None, False)

        parentpath = target._get_basepath(newpath, target.depth - 1)

        if parentpath:
            self._increment_numchild(parentpath)

        # saving the instance before returning it
        newobj.path = newpath
        newobj.save(using=self._db)

        return newobj

    @transaction.atomic
    def move(self, node, target, pos=None):
        """
        Moves node and all its descendants to a new position
        relative to another node.

        :raise PathOverflow: when the library can't make room for the
           node's new position
        """
        pos = self._prepare_pos_var_for_move(pos)

        # initialize variables and if moving to a child, updates "move to
        # child" to become a "move to sibling" if possible (if it can't
        # be done, it means that we are  adding the first child)
        target, newdepth, siblings, newpos, pos = self.update_move_to_child_vars(node=node, target=target, pos=pos)

        if target.is_descendant_of(node):
            raise InvalidMoveToDescendant(_("Can't move node to a descendant."))

        if pos == "sorted-sibling":
            siblings = self.get_sorted_pos_queryset(self.get_siblings(target), node)
            if first := siblings.first():
                newpos = first._get_lastpos_in_path()
                node_parentpath = node._get_basepath(node.path, node.depth - 1)
                first_parentpath = first._get_basepath(first.path, first.depth - 1)
                if node_parentpath == first_parentpath and node._get_lastpos_in_path() == newpos - 1:
                    # The node is already in the right place (same parent,
                    # right before the computed sibling), nothing to do
                    return
            else:
                newpos = None
                pos = "last-sibling"

        # Handle special cases where nothing needs to be done
        if newdepth == node.get_depth():
            if pos == "first-sibling" and node == self.get_first_sibling(target):
                return

            if pos == "last-sibling" and node == self.get_last_sibling(target):
                return

        if node == target and (
            # Moving a node to left/right of itself is a noop
            pos == "left" or (pos in ("right", "last-sibling") and target == self.get_last_sibling(target))
        ):
            return

        # Move nodes
        oldpath, newpath = self._reorder_nodes_before_add_or_move(
            pos, newpos, newdepth, target, siblings, node.path, True
        )

        self.update_parent_counts_after_move(oldpath, newpath)
        node.refresh_from_db()  # Node path and depth will have changed
        target.refresh_from_db()

    def update_parent_counts_after_move(self, oldpath, newpath):
        """
        Update the numchild value of parent nodes after performing a move.
        """
        oldparentpath = self.model._get_parent_path_from_path(oldpath)
        newparentpath = self.model._get_parent_path_from_path(newpath)
        if oldparentpath != newparentpath:
            # node changed parent, updating counts
            if oldparentpath:
                self._decrement_numchild(oldparentpath)
            if newparentpath:
                self._increment_numchild(newparentpath)

    def update_move_to_child_vars(self, node, target, pos):
        """Update preliminary vars in :meth:`move` when moving to a child"""
        newdepth = target.depth
        newpos = None
        siblings = []
        if pos in ("first-child", "last-child", "sorted-child"):
            if target == node:
                raise InvalidMoveToDescendant(_("Can't move node to itself."))

            # moving to a child
            parent = target
            newdepth += 1
            if target.is_leaf():
                # moving as a target's first child
                newpos = 1
                pos = "first-sibling"
                siblings = self.tree_model.objects.none()
            else:
                target = self.get_last_child(target)
                pos = {
                    "first-child": "first-sibling",
                    "last-child": "last-sibling",
                    "sorted-child": "sorted-sibling",
                }[pos]

            # this is not for save(), since if needed, will be handled with a
            # custom UPDATE, this is only here to update django's object,
            # should be useful in loops
            parent.numchild += 1

        return target, newdepth, siblings, newpos, pos

    @transaction.atomic
    def move_bulk(self, moves):
        """
        Moves several nodes (and all their descendants) to new positions in a
        single, atomic operation.

        Unlike issuing several :meth:`move` calls, the final tree state is
        planned and validated up front from the *resulting* relationships, so
        the caller never has to guess an execution order. Cases that are legal
        in the final tree but impossible to reach with sequential single moves
        (e.g. swapping two subtrees, or moving a node under a node that is
        itself moved) are supported.

        :param moves: An iterable of move descriptions. Each description is a
            dict with the keys:

            - ``node``: the node (instance or primary key) to move.
            - ``target``: the reference node (instance or primary key), or
              ``None`` to place the node at root level.
            - ``pos``: one of ``first-child``, ``last-child``,
              ``first-sibling``, ``last-sibling``, ``left``, ``right`` (or the
              ``sorted-*`` equivalents when :attr:`node_order_by` is set).
              Defaults to ``last-sibling``.

            When several moves resolve to the same final slot, they are placed
            in the order in which they appear in ``moves``.

        :returns: A list of :class:`BulkMoveResult` objects, one per requested
            move, in the same order.

        :raise InvalidPosition: when a node is listed more than once, a target
           or node does not exist, relative anchors cannot be resolved, or an
           invalid ``pos`` is passed.
        :raise InvalidMoveToDescendant: when the final state moves a node under
           one of its own descendants.
        :raise PathOverflow: when the final state exceeds the path capacity.

        The whole batch is validated before any row is written, and any failure
        rolls the entire transaction back. On success, the in-memory instances
        passed in as ``node``/``target`` are refreshed from the database
        (including their cached parent); node objects not passed in must be
        re-fetched. :data:`path_updated` is sent once per physically relocated
        subtree, but only after the transaction has been committed.
        """
        cls = self.tree_model
        using = self._db or router.db_for_write(cls)

        plan, instances = self._build_move_bulk_plan(moves, using)

        # ---- write phase: park every subtree at an unused root-level step,
        # then drop each subtree onto its final path. This staging pass makes
        # the order of writes irrelevant for the unique path constraint, so
        # swaps and other permutations never hit temporary conflicts.
        net_changes = []  # (old_path, new_path)

        staged = {}  # node_pk -> (original_path, staging_path)
        for node_pk in plan["stage_order"]:
            original_path = plan["original_paths"][node_pk]
            staging_path = plan["staging_paths"][node_pk]
            self._rewrite_subtree_paths(original_path, staging_path, using=using)
            staged[node_pk] = (original_path, staging_path)

        for group in plan["affected_groups"]:
            for node_pk in group["final_children"]:
                if node_pk not in staged:
                    # kept its slot: nothing to write
                    continue
                staging_path = staged[node_pk][1]
                final_path = plan["final_paths"][node_pk]
                self._rewrite_subtree_paths(staging_path, final_path, using=using)
                net_changes.append((plan["original_paths"][node_pk], final_path))

        for parent_pk, numchild in plan["numchild_updates"].items():
            cls.objects.using(using).filter(pk=parent_pk).update(numchild=numchild)

        # Refresh the node/target instances the caller is holding so their
        # path/depth/numchild/_cached_parent_obj are consistent with the DB.
        refreshed = {}
        for node_pk, instance in instances.items():
            if node_pk in refreshed:
                continue
            instance.refresh_from_db(using=using)
            refreshed[node_pk] = instance

        # Signals describe only the net change (old committed path -> new path),
        # never the temporary staging paths, and are emitted post-commit.
        net_changes.sort(key=lambda change: len(change[0]), reverse=True)

        def _send_path_signals():
            for old_path, new_path in net_changes:
                path_updated.send(sender=cls, old_path=old_path, new_path=new_path, using=using)

        transaction.on_commit(_send_path_signals)

        return plan["results"]

    # ------------------------------------------------------------------
    # Bulk move planning
    # ------------------------------------------------------------------

    def _build_move_bulk_plan(self, moves, using):
        """
        Validates the requested batch against the *final* tree state and builds
        the write plan. No rows are modified here.
        """
        cls = self.tree_model
        child_positions = ("first-child", "last-child", "sorted-child")
        root_positions = ("first-sibling", "last-sibling", "sorted-sibling")

        normalized = []
        self_relative_noops = set()
        instances = {}
        seen_node_pks = set()
        required_pks = set()

        for move in moves:
            if not isinstance(move, dict) or set(move) - {"node", "target", "pos"}:
                raise InvalidPosition("Each move must be a dict with 'node', 'target' and 'pos' keys.")
            if "node" not in move:
                raise InvalidPosition("Each move must specify the 'node' to move.")

            node_ref = move["node"]
            target_ref = move.get("target")
            pos = self._prepare_pos_var_for_move(move.get("pos"))

            node_pk = node_ref.pk if isinstance(node_ref, cls) else node_ref
            if node_pk is None:
                raise InvalidPosition("Cannot move a node that was not saved to the database.")
            if node_pk in seen_node_pks:
                raise InvalidPosition(f"Node {node_pk} is listed more than once in the batch.")
            seen_node_pks.add(node_pk)
            required_pks.add(node_pk)
            if isinstance(node_ref, cls):
                instances[node_pk] = node_ref

            target_pk = None
            if isinstance(target_ref, cls):
                target_pk = target_ref.pk
                instances.setdefault(target_pk, target_ref)
            elif target_ref is not None:
                target_pk = target_ref
            if target_pk is not None:
                required_pks.add(target_pk)
            elif pos not in root_positions:
                raise InvalidPosition(f"Cannot position a root node with '{pos}'.")

            if target_pk == node_pk and pos in child_positions:
                raise InvalidMoveToDescendant(_("Can't move node to itself."))
            if target_pk == node_pk and pos in ("left", "right"):
                # left/right of oneself never changes anything
                self_relative_noops.add(node_pk)

            normalized.append({"node_pk": node_pk, "target_pk": target_pk, "pos": pos})

        if not normalized:
            return (
                {
                    "results": [],
                    "stage_order": [],
                    "staging_paths": {},
                    "original_paths": {},
                    "final_paths": {},
                    "affected_groups": [],
                    "numchild_updates": {},
                },
                instances,
            )

        # Lock every node taking part in the batch so a concurrent editor
        # cannot change parent/position while the plan is being applied.
        list(cls.objects.using(using).select_for_update().filter(pk__in=required_pks))
        nodes = self._load_node_data(cls, using, required_pks)
        missing = required_pks - set(nodes)
        if missing:
            raise InvalidPosition(f"Nodes not found: {sorted(missing, key=str)}")

        # Cache of pk -> (path, depth) for stationary nodes inspected while
        # walking parents during cycle detection.
        extra_cache = {}

        def get_data(pk):
            if pk in nodes:
                return nodes[pk]
            return extra_cache.setdefault(pk, self._load_node_data(cls, using, [pk])[pk])

        def current_parent_pk(pk):
            data = get_data(pk)
            if data["depth"] == 1:
                return None
            parent_path = cls._get_parent_path_from_path(data["path"])
            return self._pk_for_path(cls, using, parent_path)

        move_by_pk = {move["node_pk"]: move for move in normalized}

        # Final parent pk of every moved node (None = root level).
        #
        # A *child* placement points straight at its target. A *sibling*
        # placement points at the target's parent: a stationary target keeps
        # its current parent, while a moved target defers to that target's own
        # final parent ("sibling-of" placeholder). Following those placeholders
        # reaches either a concrete parent, or a loop of sibling references
        # (e.g. two roots swapping). Such a loop is legal and every member
        # shares the concrete parent that the loop's moves point back into.
        final_parent = {}
        for move in normalized:
            node_pk, target_pk, pos = move["node_pk"], move["target_pk"], move["pos"]
            if pos in child_positions:
                final_parent[node_pk] = target_pk
            elif target_pk is None:
                final_parent[node_pk] = None
            elif target_pk not in seen_node_pks:
                final_parent[node_pk] = current_parent_pk(target_pk)
            else:
                final_parent[node_pk] = ("sibling-of", target_pk)

        def resolve_parent(start_pk):
            """
            Follows sibling-of placeholders until a concrete parent is
            reached. Returns ``(parent_pk, loop)``; ``loop`` is the list of
            members only when the references close into a pure sibling loop
            (legal: they share one parent group).
            """
            order = []
            seen = set()
            pk = start_pk
            while True:
                value = final_parent[pk]
                if not (isinstance(value, tuple) and value[0] == "sibling-of"):
                    return value, None
                if pk in seen:
                    return None, order[order.index(pk):]
                seen.add(pk)
                order.append(pk)
                pk = value[1]

        # Resolve everything that leads (possibly through other moved nodes) to
        # a concrete parent. Pure sibling loops are resolved afterwards.
        for node_pk in list(final_parent):
            if not isinstance(final_parent[node_pk], tuple):
                continue
            parent_pk, loop = resolve_parent(node_pk)
            if loop is None:
                final_parent[node_pk] = parent_pk

        # Remaining placeholders belong to sibling loops (e.g. a root swap).
        # All members share the parent of the first loop member whose target is
        # stationary or root-level; root-level is the default for a closed loop.
        while any(isinstance(value, tuple) for value in final_parent.values()):
            member = next(pk for pk, value in final_parent.items() if isinstance(value, tuple))
            _parent, loop = resolve_parent(member)
            shared_parent = None
            for loop_pk in loop:
                target_pk = move_by_pk[loop_pk]["target_pk"]
                if target_pk is None:
                    shared_parent = None
                    break
                if target_pk not in seen_node_pks:
                    shared_parent = current_parent_pk(target_pk)
                    break
            for loop_pk in loop:
                final_parent[loop_pk] = shared_parent

        # Walk each node's final ancestry. A node ending up under its own
        # subtree is a cycle, even when its ancestor is a stationary node whose
        # current path happens to sit below it.
        path_cache = {pk: nodes[pk]["path"] for pk in nodes}

        def path_of(pk):
            if pk in path_cache:
                return path_cache[pk]
            return extra_cache.setdefault(pk, self._load_node_data(cls, using, [pk])[pk])["path"]

        for move in normalized:
            node_pk = move["node_pk"]
            node_path = nodes[node_pk]["path"]
            ancestor_pk = final_parent[node_pk]
            visited = set()
            while ancestor_pk is not None:
                if ancestor_pk == node_pk:
                    raise InvalidMoveToDescendant(_("Can't move node to a descendant."))
                if ancestor_pk in visited:
                    raise InvalidMoveToDescendant(_("Can't move node to a descendant."))
                visited.add(ancestor_pk)
                if ancestor_pk not in seen_node_pks:
                    # Stationary ancestor: it keeps its current subtree, so if
                    # it currently sits under the moved node the final state
                    # would put the node under one of its own descendants.
                    ancestor_path = path_of(ancestor_pk)
                    if ancestor_path.startswith(node_path):
                        raise InvalidMoveToDescendant(_("Can't move node to a descendant."))
                    break
                # Moved ancestor: keep walking final-parent edges; a closed loop
                # (caught by `visited`) is the only possible cycle here.
                ancestor_pk = final_parent[ancestor_pk]

        # Final sibling groups -----------------------------------------
        affected_parent_pks = set()
        for move in normalized:
            node_pk = move["node_pk"]
            affected_parent_pks.add(None if nodes[node_pk]["depth"] == 1 else current_parent_pk(node_pk))
            affected_parent_pks.add(final_parent[node_pk])

        old_children = {}
        child_data = {}
        for parent_pk in affected_parent_pks:
            children = self._load_children(cls, using, parent_pk, extra_cache)
            old_children[parent_pk] = [child["pk"] for child in children]
            for child in children:
                child_data[child["pk"]] = child

        final_groups = {}
        for parent_pk in affected_parent_pks:
            ordered = [
                pk
                for pk in old_children[parent_pk]
                if pk not in seen_node_pks or pk in self_relative_noops
            ]
            pending = []
            for move in normalized:
                if final_parent[move["node_pk"]] != parent_pk:
                    continue
                node_pk, target_pk, pos = move["node_pk"], move["target_pk"], move["pos"]
                if node_pk in self_relative_noops:
                    # left/right of oneself never changes the tree
                    continue
                if pos in ("first-child", "first-sibling"):
                    ordered.insert(0, node_pk)
                elif pos in ("last-child", "last-sibling"):
                    ordered.append(node_pk)
                else:  # left / right / sorted-* (sorted positions are reordered below)
                    pending.append(move)

            if cls.node_order_by:
                for move in pending:
                    if move["pos"].startswith("sorted") and move["node_pk"] not in ordered:
                        ordered.append(move["node_pk"])
                data_for_sort = {}
                data_for_sort.update(child_data)
                data_for_sort.update(nodes)
                rank = {pk: i for i, pk in enumerate(ordered)}
                ordered = self._sort_sorted_group(cls, ordered, data_for_sort, rank)
                pending = [move for move in pending if move["pos"] in ("left", "right")]

            # Resolve left/right anchors to a concrete index. Every moved node
            # is placed exactly once; anchors may be stationary, already placed
            # by a positional move, or another pending node. Input order breaks
            # ties and makes placements deterministic.
            unresolved = pending
            while unresolved:
                still_unresolved = []
                progressed = False
                for move in unresolved:
                    node_pk, target_pk, pos = move["node_pk"], move["target_pk"], move["pos"]
                    if target_pk in ordered:
                        if node_pk in ordered:
                            ordered.remove(node_pk)
                        index = ordered.index(target_pk)
                        ordered.insert(index + 1 if pos == "right" else index, node_pk)
                        progressed = True
                    else:
                        still_unresolved.append(move)
                if not progressed:
                    raise InvalidPosition(
                        "Could not resolve a relative move: the target sibling must end up in the same group."
                    )
                unresolved = still_unresolved

            final_groups[parent_pk] = {
                "parent_pk": parent_pk,
                "old_children": old_children[parent_pk],
                "final_children": ordered,
            }

        plan = self._finalize_move_bulk_plan(
            cls, using, normalized, nodes, final_parent, final_groups, extra_cache
        )
        return plan, instances

    def _load_node_data(self, cls, using, pks):
        values = (
            cls.objects.using(using)
            .filter(pk__in=pks)
            .values("pk", "path", "depth", *cls._bulk_order_field_names())
        )
        return {obj["pk"]: obj for obj in values}

    def _pk_for_path(self, cls, using, path):
        return cls.objects.using(using).filter(path=path).values_list("pk", flat=True).first()

    def _load_children(self, cls, using, parent_pk, extra_cache):
        if parent_pk is None:
            return list(cls.objects.using(using).filter(depth=1).order_by("path").values(
                "pk", "path", "depth", *cls._bulk_order_field_names()
            ))
        if parent_pk not in extra_cache:
            parent = cls.objects.using(using).filter(pk=parent_pk).values("path", "depth").first()
            if parent is None:
                raise InvalidPosition(f"Parent node {parent_pk} not found.")
            extra_cache[parent_pk] = parent
        parent = extra_cache[parent_pk]
        parent_path = parent["path"]
        interval = cls._get_children_path_interval(parent_path)
        return list(
            cls.objects.using(using)
            .filter(depth=parent["depth"] + 1, path__range=interval)
            .order_by("path")
            .values("pk", "path", "depth", *cls._bulk_order_field_names())
        )

    def _sort_sorted_group(self, cls, ordered, data_by_pk, fallback_rank):
        """
        Returns ``ordered`` sorted by ``node_order_by`` (Django semantics:
        nulls first, ascending unless the field is prefixed with ``-``),
        keeping the pre-existing relative order on ties.
        """
        import functools

        fields = [(f[1:] if f.startswith("-") else f, f.startswith("-")) for f in cls.node_order_by]

        @functools.cmp_to_key
        def compare(pk_a, pk_b):
            data_a, data_b = data_by_pk[pk_a], data_by_pk[pk_b]
            for name, descending in fields:
                value_a = data_a.get(name)
                value_b = data_b.get(name)
                if value_a is None and value_b is None:
                    continue
                if value_a is None:
                    return -1
                if value_b is None:
                    return 1
                if value_a == value_b:
                    continue
                less = value_a < value_b
                return -1 if (less != descending) else 1
            return -1 if fallback_rank[pk_a] < fallback_rank[pk_b] else 1

        return sorted(ordered, key=compare)

    def _finalize_move_bulk_plan(self, cls, using, normalized, nodes, final_parent, final_groups, extra_cache):
        """
        Turns the final sibling groups into concrete paths, performs the
        ordering/path-capacity checks and chooses root-level staging paths that
        never collide with current or final root positions.
        """
        moved_pks = {move["node_pk"] for move in normalized}

        affected_groups = [
            group
            for group in final_groups.values()
            if group["old_children"] != group["final_children"]
        ]

        # Final paths are computed in order of *final* parent depth, so a moved
        # parent already has its final path when a child group is processed.
        final_paths = {}

        def final_depth_of_parent(parent_pk, chain=None):
            if parent_pk is None:
                return 0
            if parent_pk in final_paths:
                return len(final_paths[parent_pk]) // cls.steplen
            if parent_pk in moved_pks:
                # Sorting by current depth would be wrong if it changes depth,
                # so compute its final depth through the final-parent chain.
                chain = chain or set()
                if parent_pk in chain:
                    raise InvalidMoveToDescendant(_("Can't move node to a descendant."))
                chain.add(parent_pk)
                return final_depth_of_parent(final_parent[parent_pk], chain) + 1
            if parent_pk in extra_cache:
                return extra_cache[parent_pk]["depth"]
            return nodes[parent_pk]["depth"]

        affected_groups.sort(key=lambda group: final_depth_of_parent(group["parent_pk"]))

        for group in affected_groups:
            parent_pk = group["parent_pk"]
            if parent_pk is None:
                parent_path = ""
            elif parent_pk in final_paths:
                # Includes both moved parents and stationary parents that were
                # reshuffled inside an affected sibling group.
                parent_path = final_paths[parent_pk]
            else:
                parent_data = extra_cache.get(parent_pk) or nodes.get(parent_pk)
                if parent_data is None:
                    parent_data = self._load_node_data(cls, using, [parent_pk])[parent_pk]
                    extra_cache[parent_pk] = parent_data
                parent_path = parent_data["path"]
            parent_depth = len(parent_path) // cls.steplen
            for index, child_pk in enumerate(group["final_children"], start=1):
                final_paths[child_pk] = cls._get_path(parent_path, parent_depth + 1, index)

        # Original paths of every subtree that genuinely changes path. Only
        # those are parked at root level, keeping the required free slots to a
        # minimum (a two-subtree swap needs just two free root codes).
        stage_roots = []
        original_paths = {}
        for group in affected_groups:
            for child_pk in group["final_children"]:
                if child_pk in nodes:
                    old_path = nodes[child_pk]["path"]
                elif child_pk in extra_cache:
                    old_path = extra_cache[child_pk]["path"]
                else:
                    old_path = cls.objects.using(using).values_list("path", flat=True).get(pk=child_pk)
                original_paths[child_pk] = old_path
                # A subtree is only relocated when its final path differs
                # from its current path; children that keep their slot are
                # left in place, which keeps the number of staging codes to a
                # minimum (a two-subtree swap needs just two free codes).
                if final_paths[child_pk] == old_path:
                    continue
                stage_roots.append((len(old_path) // cls.steplen, old_path, child_pk))

        # ---- path capacity checks against the final state -------------
        max_length = cls._meta.get_field("path").max_length
        max_step = len(cls.alphabet) ** cls.steplen - 1
        longest_suffix = self._longest_descendant_suffixes(cls, using, [item[1] for item in stage_roots])

        for _d, _o, child_pk in stage_roots:
            final_path = final_paths[child_pk]
            step_value = cls._str2int(final_path[-cls.steplen :])
            if step_value > max_step:
                raise PathOverflow(_(f"Path Overflow from: '{final_path}'"))
            if len(final_path) + longest_suffix[original_paths[child_pk]] > max_length:
                raise PathOverflow(
                    _(
                        "The new node is too deep in the tree, try"
                        " increasing the path.max_length property"
                        " and UPDATE your database"
                    )
                )

        # ---- reserve staging paths ------------------------------------
        # Every relocated subtree is parked on a fresh root-level step that is
        # unused by both the current roots and every final root position, so
        # the unique path constraint can never observe a temporary collision
        # regardless of write order. Only subtrees whose path actually changes
        # are parked, so a swap of two subtrees needs just two free codes.
        live_root_codes = {
            cls._str2int(path[-cls.steplen :])
            for path in cls.objects.using(using).filter(depth=1).values_list("path", flat=True)
        }
        final_root_codes = set()
        for group in affected_groups:
            if group["parent_pk"] is None:
                final_root_codes.update(range(1, len(group["final_children"]) + 1))
        forbidden_root_codes = live_root_codes | final_root_codes

        staging_paths = {}
        next_code = max(forbidden_root_codes) + 1 if forbidden_root_codes else 1
        # A materialized-path tree always needs a free slot to move through,
        # exactly like the single move() API: a namespace that cannot provide
        # enough temporary codes raises PathOverflow.
        ordered_stage_roots = sorted(stage_roots, key=lambda item: (-item[0], item[1]))
        for _d, old_path, child_pk in ordered_stage_roots:
            while next_code <= max_step and next_code in forbidden_root_codes:
                next_code += 1
            if next_code > max_step:
                # No free root-level slot to park the subtree. Materialized
                # paths always need a temporary free position to move through,
                # exactly like the single move() API: a fully packed namespace
                # cannot be permuted without raising PathOverflow.
                raise PathOverflow(_("Path Overflow while staging subtrees for the bulk move."))
            staging_paths[child_pk] = cls._get_path("", 1, next_code)
            forbidden_root_codes.add(next_code)
            next_code += 1

        numchild_updates = {
            group["parent_pk"]: len(group["final_children"])
            for group in affected_groups
            if group["parent_pk"] is not None
        }

        results = []
        for move in normalized:
            node_pk = move["node_pk"]
            parent_pk = final_parent[node_pk]
            # A node whose requested position matches its current position is
            # a no-op: it never enters an affected group and keeps its path.
            final_path = final_paths.get(node_pk, nodes[node_pk]["path"])
            group_children = final_groups[parent_pk]["final_children"]
            if node_pk in group_children:
                index = group_children.index(node_pk) + 1
            else:
                # unaffected group: report the node's current sibling position
                index = cls._str2int(nodes[node_pk]["path"][-cls.steplen :])
            results.append(
                BulkMoveResult(
                    node_pk=node_pk,
                    parent_pk=parent_pk,
                    new_path=final_path,
                    depth=len(final_path) // cls.steplen,
                    sibling_index=index,
                    moved=original_paths.get(node_pk, nodes[node_pk]["path"]) != final_path,
                )
            )

        plan = {
            "results": results,
            "stage_order": [
                child_pk
                for _d, _o, child_pk in sorted(stage_roots, key=lambda item: (-item[0], item[1]))
                if child_pk in staging_paths
            ],
            "staging_paths": staging_paths,
            "original_paths": original_paths,
            "final_paths": final_paths,
            "affected_groups": affected_groups,
            "numchild_updates": numchild_updates,
        }
        return plan

    def _longest_descendant_suffixes(self, cls, using, root_paths):
        """
        :returns: a mapping ``root_path -> length of the longest path suffix
                  inside that subtree that moves *with* it. Descendants that
                  are themselves staged roots (a separately moved subtree) are
                  excluded, since they carry their own capacity check.
        """
        root_set = set(root_paths)
        result = {path: 0 for path in root_paths}
        if not root_set:
            return result

        condition = Q()
        for path in root_set:
            condition |= Q(path__startswith=path)
        rows = (
            cls.objects.using(using)
            .filter(condition)
            .annotate(path_len=Length("path"))
            .values_list("path", "path_len")
        )
        # A node belongs to the longest staged-root prefix that encloses it.
        # Roots are unique paths with fixed step length, so checking the roots
        # whose length is <= the path's length (longest first) is sufficient.
        roots_by_len = collections.defaultdict(list)
        for root in root_set:
            roots_by_len[len(root)].append(root)
        for path, length in rows:
            nearest = path if path in root_set else None
            if nearest is None:
                for root_len in sorted((size for size in roots_by_len if size < length), reverse=True):
                    prefix = path[:root_len]
                    if prefix in root_set:
                        nearest = prefix
                        break
            if nearest is not None:
                result[nearest] = max(result[nearest], length - len(nearest))
        return result

    def get_root_nodes(self):
        """:returns: A queryset containing the root nodes in the tree."""
        return self.tree_model.objects.filter(depth=1).order_by("path")

    def get_descendants_group_count(self, parent=None):
        """
        Helper for a very common case: get a group of siblings and the number
        of *descendants* (not only children) in every sibling.

        :param parent:

            The parent of the siblings to return. If no parent is given, the
            root nodes will be returned.

        :returns:

            A Queryset of node objects with an extra attribute: `descendants_count`.
        """
        cls = self.tree_model

        qs = self.get_children(parent) if parent else cls.objects.get_root_nodes()
        subquery = (
            cls.objects.filter(path__startswith=OuterRef("path"))
            .order_by()
            .annotate(count=Func(F("pk"), function="Count"))
            .values("count")
        )
        qs = qs.annotate(
            descendants_count=Subquery(subquery, output_field=models.IntegerField()) - 1
        )  # Subtract the parent node from the count
        return qs

    @transaction.atomic
    def load_bulk(
        self,
        bulk_data,
        parent=None,
        keep_ids=False,
        bulk_create=False,
        batch_size=1000,
    ) -> list:
        """
        Loads a list/dictionary structure to the tree.

        :param bulk_data:

            The data that will be loaded, the structure is a list of
            dictionaries with 2 keys:

            - ``data``: will store arguments that will be passed for object
                creation, and

            - ``children``: a list of dictionaries, each one has it's own
                ``data`` and ``children`` keys (a recursive structure)

        :param parent:

            The node that will receive the structure as children, if not
            specified the first level of the structure will be loaded as root
            nodes

        :param keep_ids:

            If enabled, loads the nodes with the same primary keys that are
            given in the structure. Will error if there are nodes without
            primary key info or if the primary keys are already used.

        :param bulk_create:

            Whether to bulk create objects using Django's ``bulk_create()`` method. Only works
            for models without multi-table inheritance. Also does not work with
            MySQL and MSSQL database backends.

        :param batch_size:

            The batch size for ``bulk_create()`` when creating descendant nodes.
            Default is 1000.

        :returns: A list of the added node ids.

        The ordering of nodes in the loaded data is preserved. If this
        needs to be corrected (e.g., to cater for a new `node_order_by`)
        then `fix_tree()` can be run separately on the imported subtree.
        """

        if not bulk_create:
            return super().load_bulk(bulk_data, parent=parent, keep_ids=keep_ids)

        conn = connections[router.db_for_write(self.model)]
        if not conn.features.can_return_rows_from_bulk_insert or conn.vendor == "microsoft":
            raise ValueError("Database backend does not support bulk load. Use load_bulk without bulk_create=True.")

        added = []
        bulk_data = prepare_dumpdata_for_loading(self.model, data=bulk_data, keep_ids=keep_ids)
        children_to_create = []

        def _build_children(parent_node, children) -> None:
            child_depth = parent_node.depth + 1

            for i, child in enumerate(children):
                child.object.depth = child_depth
                child.object.numchild = len(child.children)
                child.object.path = self.model._get_path(parent_node.path, child_depth, i + 1)

                children_to_create.append(child)

                # Recursively process grandchildren
                _build_children(child.object, child.children)

        # Create first level of the bulk data using standard operations, since there may be existing siblings
        for deserialized_obj in bulk_data:
            deserialized_obj.object.numchild = len(deserialized_obj.children)  # Set numchild manually
            node_obj = (
                self.add_child(parent, instance=deserialized_obj.object)
                if parent
                else self.add_root(instance=deserialized_obj.object)
            )
            save_m2m(node_obj, deserialized_obj)
            added.append(node_obj.pk)
            _build_children(node_obj, deserialized_obj.children)

        # Bulk create descendants
        created = self.bulk_create([obj.object for obj in children_to_create], batch_size=batch_size)

        # Save m2m relationships
        for obj, source in zip(created, children_to_create):
            save_m2m(obj, source)

        added.extend([obj.pk for obj in created])

        return added

    def dump_bulk(self, parent=None, keep_ids=True):
        """Dumps a tree branch to a python data structure."""

        cls = self.tree_model

        # Because of fix_tree, this method assumes that the depth
        # and numchild properties in the nodes can be incorrect,
        # so no helper methods are used
        qset = cls.objects.all().order_by("depth", "path")
        if parent:
            qset = qset.filter(path__startswith=parent.path)
        ret, lnk = [], {}
        pk_field = cls._meta.pk.attname
        for pyobj in serializers.serialize("python", qset.iterator()):
            # django's serializer stores the attributes in 'fields'
            fields = pyobj["fields"]
            path = fields["path"]
            depth = int(len(path) / cls.steplen)
            # this will be useless in load_bulk
            del fields["depth"]
            del fields["path"]
            del fields["numchild"]
            fields.pop(pk_field, None)  # this happens immediately after a load_bulk

            newobj = {"data": fields}
            if keep_ids:
                newobj[pk_field] = pyobj["pk"]

            if (not parent and depth == 1) or (parent and len(path) == len(parent.path)):
                ret.append(newobj)
            else:
                parentpath = cls._get_basepath(path, depth - 1)
                parentobj = lnk[parentpath]
                if "children" not in parentobj:
                    parentobj["children"] = []
                parentobj["children"].append(newobj)
            lnk[path] = newobj
        return ret

    def _increment_numchild(self, path):
        self.tree_model.objects.filter(path=path).update(numchild=F("numchild") + 1)

    def _decrement_numchild(self, path):
        self.tree_model.objects.filter(path=path).update(numchild=F("numchild") - 1)

    def _reorder_nodes_before_add_or_move(
        self, pos, newpos, newdepth, target, siblings, oldpath=None, movebranch=False
    ):
        """
        Handles the reordering of nodes and branches when adding/moving
        nodes.

        :returns: A tuple containing the old path and the new path.
        """
        if (pos == "last-sibling") or (pos == "right" and target == self.get_last_sibling(target)):
            # easy, the last node
            last = self.get_last_sibling(target)
            newpath = last._inc_path()
            if movebranch:
                self._set_newpath_in_branches(oldpath, newpath)
            return oldpath, newpath

        if newpos is None:
            siblings = self.get_siblings(target)
            siblings = {
                "left": siblings.filter(path__gte=target.path),
                "right": siblings.filter(path__gt=target.path),
                "first-sibling": siblings,
            }[pos]
            basenum = target._get_lastpos_in_path()
            newpos = {"first-sibling": 1, "left": basenum, "right": basenum + 1}[pos]

        newpath = self.model._get_path(target.path, newdepth, newpos)

        # If the move is amongst siblings and is to the left and there
        # are siblings to the right of its new position then to be on
        # the safe side we temporarily dump it on the end of the list
        tempnewpath = None
        if movebranch and len(oldpath) == len(newpath):
            parentoldpath = self.model._get_basepath(oldpath, int(len(oldpath) / self.model.steplen) - 1)
            parentnewpath = self.model._get_basepath(newpath, newdepth - 1)
            if parentoldpath == parentnewpath and siblings and newpath < oldpath:
                last = self.get_last_sibling(target)
                basenum = last._get_lastpos_in_path()
                tempnewpath = self.model._get_path(newpath, newdepth, basenum + 2)
                self._set_newpath_in_branches(oldpath, tempnewpath)

        # Optimisation to only move siblings which need moving
        # (i.e. if we've got holes, allow them to compress)
        movesiblings = []
        priorpath = newpath
        for node in siblings:
            # If the path of the node is already greater than the path
            # of the previous node it doesn't need shifting
            if node.path > priorpath:
                break
            # It does need shifting, so add to the list
            movesiblings.append(node)
            # Calculate the path that it would be moved to, as that's
            # the next "priorpath"
            priorpath = node._inc_path()
        movesiblings.reverse()

        for node in movesiblings:
            # moving the siblings (and their branches) at the right of the
            # related position one step to the right
            _inc_path = node._inc_path()
            self._set_newpath_in_branches(node.path, node._inc_path())

            if movebranch:
                if oldpath.startswith(node.path):
                    # if moving to a parent, update oldpath since we just
                    # increased the path of the entire branch
                    oldpath = _inc_path + oldpath[len(_inc_path) :]
                if target.path.startswith(node.path):
                    # and if we moved the target, update the object
                    # django made for us, since the update won't do it
                    # maybe useful in loops
                    target.path = _inc_path + target.path[len(_inc_path) :]
        if movebranch:
            # node to move
            self._set_newpath_in_branches(tempnewpath or oldpath, newpath)
        return oldpath, newpath

    def _rewrite_subtree_paths(self, oldpath, newpath, using=None, emit_signal=False):
        """
        Rewrites ``oldpath`` (and every descendant path) to ``newpath`` in a
        single UPDATE statement, also adjusting ``depth`` when the path length
        changes.

        :param emit_signal: When True, sends :data:`path_updated` immediately.
            Bulk operations pass False here and send their own signals only
            after the surrounding transaction has been committed.

        :returns: The alias of the database the update ran on.
        """
        new_path_value = Concat(Value(newpath), Substr("path", len(oldpath) + 1))
        update_kwargs = {}

        # Warning: MySQL processes multiple assigments left to right, using the updated value
        # for any column that is referenced in a subsequent assignment. This behavior differs from standard SQL.
        # See https://dev.mysql.com/doc/refman/8.4/en/update.html
        # For a table with schema name (VARCHAR), length (INT) and row (name="bob", length=3), the query:
        # `UPDATE table SET name='alice', length=LENGTH(name);`
        # would set `length` to 5 in MySQL, but 3 on other databases, because they use the original source value.
        # To avoid having to special case for MySQL, we need to supply the depth as the first parameter to
        # update_kwargs.

        if len(oldpath) != len(newpath):
            update_kwargs["depth"] = Length(new_path_value) / self.model.steplen
        update_kwargs["path"] = new_path_value

        model = self.tree_model
        queryset = model.objects.using(using).filter(path__startswith=oldpath) if using else model.objects.filter(
            path__startswith=oldpath
        )
        update_count = queryset.update(**update_kwargs)
        db_alias = queryset.db
        if emit_signal and update_count > 0:
            path_updated.send(sender=model, old_path=oldpath, new_path=newpath, using=db_alias)
        return db_alias

    def _set_newpath_in_branches(self, oldpath, newpath):
        """
        .. note::

           The query will only update depth values if needed.

        """
        self._rewrite_subtree_paths(oldpath, newpath, emit_signal=True)

    def get_children(self, node):
        """:returns: A queryset of all the node's children"""
        if node.is_leaf():
            return self.tree_model.objects.none()

        # Using the path interval generates a more efficient database query than just specifying a path prefix
        return self.tree_model.objects.filter(
            depth=node.depth + 1, path__range=node._get_children_path_interval(node.path)
        ).order_by("path")

    def get_children_count(self, node):
        """
        :returns: The number the node's children, calculated in the most
        efficient possible way.
        """
        return node.numchild

    def get_siblings(self, node):
        """
        :returns: A queryset of all the node's siblings, including the node
            itself.
        """
        qset = self.tree_model.objects.filter(depth=node.depth).order_by("path")
        if node.depth > 1:
            # making sure the non-root nodes share a parent
            parentpath = node._get_basepath(node.path, node.depth - 1)
            qset = qset.filter(path__range=node._get_children_path_interval(parentpath))
        return qset

    def get_descendants(self, node, include_self=False, max_depth: int | None = None):
        """
        :returns: A queryset of all the node's descendants as DFS, doesn't
            include the node itself if `include_self` is False

            If max_depth is set then the tree is limited to the specified depth relative
            to the node.
        """
        manager = self.tree_model.objects
        if include_self:
            return manager.get_tree(node, max_depth=max_depth)
        if node.is_leaf():
            return manager.none()
        return manager.get_tree(node, max_depth=max_depth).exclude(pk=node.pk)

    def get_root(self, node):
        """:returns: the root node for the current node object."""
        if node.is_root():
            return node

        return self.tree_model.objects.get(path=node.path[0 : node.steplen])

    def get_parent(self, node, update=False):
        """
        :returns: the parent node of the current node object.
            Caches the result in the object itself to help in loops.
        """
        depth = int(len(node.path) / node.steplen)
        if depth <= 1:
            return
        try:
            if update:
                del node._cached_parent_obj
            else:
                return node._cached_parent_obj
        except AttributeError:
            pass
        parentpath = node._get_basepath(node.path, depth - 1)
        node._cached_parent_obj = self.tree_model.objects.get(path=parentpath)
        return node._cached_parent_obj

    def get_ancestors(self, node):
        """
        :returns: A queryset containing the current node object's ancestors,
            starting by the root node and descending to the parent.
        """
        if node.is_root():
            return self.tree_model.objects.none()

        # This is necessary to ensure the index is used as opposed to a table scan
        paths = [node.path[0:pos] for pos in range(0, len(node.path), node.steplen)[1:]]
        return self.tree_model.objects.filter(path__in=paths).order_by("depth")

    def get_next_sibling(self, node):
        """
        :returns: The node's next sibling, or None if it was the rightmost
            sibling.
        """
        return self.get_siblings(node).filter(path__gt=node.path).first()

    def get_prev_sibling(self, node):
        """
        :returns: The node's previous sibling, or None if it was the leftmost
            sibling.
        """
        return self.get_siblings(node).filter(path__lt=node.path).last()


class MP_AddRootHandler:
    def __init__(self, cls, **kwargs):
        warnings.warn(
            "MP_AddRootHandler is deprecated. Use Node.objects.add_root() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        self.cls = cls
        self.kwargs = kwargs

    def process(self):
        instance = self.kwargs.pop("instance", None)
        return self.cls.objects.add_root(self.kwargs, instance=instance)


class MP_AddChildHandler:
    def __init__(self, node, creation_kwargs: dict[str, Any]):
        warnings.warn(
            "MP_AddChildHandler is deprecated. Use Node.objects.add_child() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        self.node = node
        self.kwargs = creation_kwargs

    def process(self):
        instance = self.kwargs.pop("instance", None)
        return self.node.__class__.objects.add_child(self.node, self.kwargs, instance=instance)


class MP_AddSiblingHandler:
    def __init__(self, node, pos, creation_kwargs: dict[str, Any]):
        warnings.warn(
            "MP_AddSiblingHandler is deprecated. Use Node.objects.add_child() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        self.node = node
        self.pos = pos
        self.kwargs = creation_kwargs

    def process(self):
        instance = self.kwargs.pop("instance", None)
        return self.node.__class__.objects.add_sibling(self.node, self.pos, self.kwargs, instance=instance)


class MP_MoveHandler:
    def __init__(self, node, target, pos=None):
        warnings.warn(
            "MP_MoveHandler is deprecated. Use Node.objects.move() instead.", RemovedInTreebeard8Warning, stacklevel=2
        )
        self.node = node
        self.node_cls = node.__class__
        self.target = target
        self.pos = pos

    def process(self):
        return self.node.__class__.objects.move(self.node, self.target, self.pos)


class MP_Node(Node):
    """Abstract model to create your own Materialized Path Trees."""

    steplen = 4
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    node_order_by = []
    path = models.CharField(max_length=255, unique=True)
    depth = models.PositiveIntegerField()
    numchild = models.PositiveIntegerField(default=0)

    TREEBEARD_IDENTIFYING_FIELD = "path"
    MOVENODE_FORM_EXCLUDED_FIELDS = ("path", "depth", "numchild")
    _DEFAULT_TREEBEARD_MANAGER = MP_NodeManager

    objects = MP_NodeManager()

    numconv_obj_ = None

    _cached_attributes = (
        *Node._cached_attributes,
        "_cached_parent_obj",
    )

    @classmethod
    def _int2str(cls, num):
        return cls.numconv_obj().int2str(num)

    @classmethod
    def _str2int(cls, num):
        return cls.numconv_obj().str2int(num)

    @classmethod
    @cache
    def numconv_obj(cls):
        return NumConv(cls.alphabet)

    def get_depth(self):
        """:returns: the depth (level) of the node"""
        return self.depth

    def is_sibling_of(self, node):
        """
        :returns: ``True`` if the node is a sibling of another node given as an
            argument, else, returns ``False``
        """
        if self.depth != node.depth:
            return False

        if self.depth == 1:
            return True  # Root nodes are always siblings

        # making sure the non-root nodes share a parent
        return node.path.startswith(self._get_basepath(self.path, self.depth - 1))

    def is_child_of(self, node):
        """
        :returns: ``True`` is the node if a child of another node given as an
            argument, else, returns ``False``
        """
        return self.path.startswith(node.path) and self.depth == node.depth + 1

    def is_descendant_of(self, node):
        """
        :returns: ``True`` if the node is a descendant of another node given
            as an argument, else, returns ``False``
        """
        return self.path.startswith(node.path) and self.depth > node.depth

    def is_root(self):
        """:returns: True if the node is a root node (else, returns False)"""
        return self.depth == 1

    def is_leaf(self):
        """:returns: True if the node is a leaf node (else, returns False)"""
        return self.numchild == 0

    @classmethod
    def _get_basepath(cls, path, depth):
        """:returns: The base path of another path up to a given depth"""
        if path:
            return path[0 : depth * cls.steplen]
        return ""

    @classmethod
    def _get_path(cls, path, depth, newstep):
        """
        Builds a path given some values

        :param path: the base path
        :param depth: the depth of the  node
        :param newstep: the value (integer) of the new step
        """
        parentpath = cls._get_basepath(path, depth - 1)
        key = cls._int2str(newstep)
        return f"{parentpath}{cls.alphabet[0] * (cls.steplen - len(key))}{key}"

    def _inc_path(self):
        """:returns: The path of the next sibling of a given node path."""
        newpos = self._str2int(self.path[-self.steplen :]) + 1
        key = self._int2str(newpos)
        if len(key) > self.steplen:
            raise PathOverflow(_(f"Path Overflow from: '{self.path}'"))
        return f"{self.path[: -self.steplen]}{self.alphabet[0] * (self.steplen - len(key))}{key}"

    def _get_lastpos_in_path(self):
        """:returns: The integer value of the last step in a path."""
        return self._str2int(self.path[-self.steplen :])

    @classmethod
    def _bulk_order_field_names(cls):
        """:returns: node_order_by field names without the descending ``-`` prefix."""
        return [field[1:] if field.startswith("-") else field for field in cls.node_order_by]

    @classmethod
    def _get_parent_path_from_path(cls, path):
        """:returns: The parent path for a given path"""
        if path:
            return path[0 : len(path) - cls.steplen]
        return ""

    @classmethod
    def _get_children_path_interval(cls, path):
        """:returns: An interval of all possible children paths for a node."""
        return (path + cls.alphabet[0] * cls.steplen, path + cls.alphabet[-1] * cls.steplen)

    class Meta:
        """Abstract model."""

        abstract = True
