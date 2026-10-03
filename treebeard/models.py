"""Models and base API"""

import operator
import warnings
from collections.abc import Iterable
from contextlib import suppress
from functools import reduce

from django.core import checks
from django.db import models, transaction
from django.db.models import Q
from django.utils.encoding import is_protected_type
from django.utils.functional import cached_property

from treebeard.deprecation import RemovedInTreebeard8Warning, get_moved_manager_classmethod, get_moved_manager_method
from treebeard.exceptions import InvalidPosition, MissingNodeOrderBy
from treebeard.utils import prepare_dumpdata_for_loading, save_m2m


class Node(models.Model):
    """Node class"""

    # Subclasses must override this to provide the name of a field
    # that identifies the model.
    TREEBEARD_IDENTIFYING_FIELD = None
    # Fields to be excluded from MoveNodeForm
    MOVENODE_FORM_EXCLUDED_FIELDS = ()
    _cached_attributes = ()

    def get_depth(self):  # pragma: no cover
        """:returns: the depth (level) of the node"""
        raise NotImplementedError

    def is_root(self):
        """:returns: True if the node is a root node (else, returns False)"""
        raise NotImplementedError

    def is_leaf(self):
        """:returns: True if the node is a leaf node (else, returns False)"""
        return not self.__class__.objects.get_children(self).exists()

    def is_sibling_of(self, node):
        """
        :returns: ``True`` if the node is a sibling of the supplied node, otherwise ``False``
        """
        return self.__class__.objects.get_siblings(self).filter(pk=node.pk).exists()

    def is_child_of(self, node):
        """
        :returns: ``True`` if the node is a child of the supplied node, otherwise ``False``
        """
        return self.__class__.objects.get_children(node).filter(pk=self.pk).exists()

    def is_descendant_of(self, node):  # pragma: no cover
        """
        :returns: ``True`` if the node is a descendant of the supplied node, otherwise ``False``
        """
        raise NotImplementedError

    @classmethod
    def add_root(cls, **kwargs):
        warnings.warn(
            f"Using {cls.__name__}.add_root() is deprecated. Use {cls.__name__}.objects.add_root() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        instance = kwargs.pop("instance", None)
        return cls.objects.add_root(create_kwargs=kwargs, instance=instance)

    def add_child(self, **kwargs):
        cls = self.__class__
        warnings.warn(
            f"Using {cls.__name__}.add_child() is deprecated. Use {cls.__name__}.objects.add_child(target) instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        instance = kwargs.pop("instance", None)
        return self.__class__.objects.add_child(self, create_kwargs=kwargs, instance=instance)

    def add_sibling(self, pos=None, **kwargs):  # pragma: no cover
        cls = self.__class__
        warnings.warn(
            f"Using {cls.__name__}.add_sibling() is deprecated. Use {cls.__name__}.objects.add_sibling() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        instance = kwargs.pop("instance", None)
        return self.__class__.objects.add_sibling(self, pos, create_kwargs=kwargs, instance=instance)

    def move(self, target, pos=None):  # pragma: no cover
        cls = self.__class__
        warnings.warn(
            f"Using {cls.__name__}.move() is deprecated. Use {cls.__name__}.objects.move() instead.",
            RemovedInTreebeard8Warning,
            stacklevel=2,
        )
        return cls.objects.move(self, target, pos)

    def delete(self, *args, **kwargs):
        """Removes a node and all it's descendants."""
        # Call the queryset delete method, which handles deletion of descendants
        return self.__class__.objects.filter(pk=self.pk).delete(*args, **kwargs)

    delete.alters_data = True
    delete.queryset_only = True

    def _clear_cached_attributes(self):
        for attr in self._cached_attributes:
            with suppress(AttributeError):
                delattr(self, attr)

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._clear_cached_attributes()

    @classmethod
    def check(cls, **kwargs):
        errors = super().check(**kwargs)
        manager_cls = cls._default_manager.__class__
        # Raise an error if the default manager for the model doesn't subclass Treebeard's manager
        if not issubclass(manager_cls, cls._DEFAULT_TREEBEARD_MANAGER):
            errors.append(
                checks.Error(
                    f"{manager_cls.__module__}.{manager_cls.__name__} does not subclass "
                    f"{cls._DEFAULT_TREEBEARD_MANAGER.__module__}.{cls._DEFAULT_TREEBEARD_MANAGER.__name__}.",
                    obj=manager_cls,
                    id="treebeard.E001",
                )
            )
        return errors

    class Meta:
        """Abstract model."""

        abstract = True


# Deprecated class methods that have moved to the model manager. Will be removed in Treebeard 8
def _inject_moved_method_back_compat():
    moved_classmethods = [
        "load_bulk",
        "dump_bulk",
        "get_root_nodes",
        "get_first_root_node",
        "get_last_root_node",
        "find_problems",
        "fix_tree",
        "get_tree",
        "get_descendants_group_count",
        "get_annotated_list_qs",
        "get_annotated_list",
    ]

    for method in moved_classmethods:
        setattr(Node, method, get_moved_manager_classmethod(method))

    moved_methods = [
        "get_children",
        "get_children_count",
        "get_siblings",
        "get_descendants",
        "get_descendant_count",
        "get_first_child",
        "get_last_child",
        "get_first_sibling",
        "get_last_sibling",
        "get_prev_sibling",
        "get_next_sibling",
        "get_root",
        "get_parent",
        "get_ancestors",
    ]

    for method in moved_methods:
        setattr(Node, method, get_moved_manager_method(method))


_inject_moved_method_back_compat()


class NodeManager(models.Manager):
    @cached_property
    def tree_model(self):
        """
        Determine what class we should use for the
        nodes returned by its tree methods (such as get_children).

        Usually this will be trivially the same as the initial model class,
        but there are special cases when model inheritance is in use:

        * If the model extends another via multi-table inheritance, we need to
        use whichever ancestor originally implemented the tree behaviour (i.e.
        the one which defines the fields used by Treebeard). We can't use the
        subclass, because it's not guaranteed that the other nodes reachable
        from the current one will be instances of the same subclass.

        * If the model is a proxy model, the returned nodes should also use
        the proxy class.
        """
        cls = self.model
        base_class = cls._meta.get_field(cls.TREEBEARD_IDENTIFYING_FIELD).model
        if cls._meta.proxy_for_model == base_class:
            return cls

        return base_class

    def get_tree(self, parent=None, max_depth: int | None = None):  # pragma: no cover
        """
        :returns:

            A queryset of nodes ordered as DFS, including the parent. If
            no parent is given, the entire tree is returned.

            If max_depth is set then the tree is limited to the specified depth, relative
            to the parent (or the root if no parent is specified).
        """
        raise NotImplementedError

    def add_root(self, create_kwargs=None, *, instance=None):  # pragma: no cover
        """
        Adds a root node to the tree. The new root node will be the new
        rightmost root node. If you want to insert a root node at a specific
        position, use :meth:`add_sibling` in an already existing root node
        instead.

        :param `create_kwargs`: dictionary of values to set on the model object.
        :param instance: Instead of passing `create_kwargs`, you can
            pass an already-constructed (but not yet saved) model instance to
            be inserted into the tree.

        :returns: the created node object. It will be save()d by this method.

        :raise NodeAlreadySaved: when the passed ``instance`` already exists
            in the database
        """
        raise NotImplementedError

    def add_child(self, target, create_kwargs=None, *, instance=None):  # pragma: no cover
        """
        Adds a child to the node. The new node will be the new rightmost
        child. If you want to insert a node at a specific position,
        use the :meth:`add_sibling` method of an already existing
        child node instead.

        :param `create_kwargs`: dictionary of values to set on the model object.
        :param instance: Instead of passing object creation data, you can
            pass an already-constructed (but not yet saved) model instance to
            be inserted into the tree.

        :returns: The created node object. It will be save()d by this method.

        :raise NodeAlreadySaved: when the passed ``instance`` already exists
            in the database
        """
        raise NotImplementedError

    def add_sibling(self, target, pos=None, create_kwargs=None, *, instance=None):  # pragma: no cover
        """
        Adds a new node as a sibling to the current node object.

        :param pos:
            The position, relative to the current node object, where the
            new node will be inserted, can be one of:

            - ``first-sibling``: the new node will be the new leftmost sibling
            - ``left``: the new node will take the node's place, which will be
              moved to the right 1 position
            - ``right``: the new node will be inserted at the right of the node
            - ``last-sibling``: the new node will be the new rightmost sibling
            - ``sorted-sibling``: the new node will be at the right position
              according to the value of node_order_by

        :param `create_kwargs`: dictionary of values to set on the model object.
        :param instance: Instead of passing object creation data, you can
            pass an already-constructed (but not yet saved) model instance to
            be inserted into the tree.

        :returns:

            The created node object. It will be saved by this method.

        :raise InvalidPosition: when passing an invalid ``pos`` parm
        :raise InvalidPosition: when :attr:`node_order_by` is enabled and the
           ``pos`` parm wasn't ``sorted-sibling``
        :raise MissingNodeOrderBy: when passing ``sorted-sibling`` as ``pos``
           and the :attr:`node_order_by` attribute is missing
        :raise NodeAlreadySaved: when the passed ``instance`` already exists
            in the database
        """
        raise NotImplementedError

    def move(self, node, target, pos=None):  # pragma: no cover
        """
        Moves a node and all it's descendants to a new position relative to another node.

        :param node:

            The node to move.

        :param target:

            The node that will be used as a relative child/sibling when moving

        :param pos:

            The position, relative to the target node, where the
            current node object will be moved to, can be one of:

            - ``first-child``: the node will be the new leftmost child of the
              ``target`` node
            - ``last-child``: the node will be the new rightmost child of the
              ``target`` node
            - ``sorted-child``: the new node will be moved as a child of the
              ``target`` node according to the value of :attr:`node_order_by`
            - ``first-sibling``: the node will be the new leftmost sibling of
              the ``target`` node
            - ``left``: the node will take the ``target`` node's place, which
              will be moved to the right 1 position
            - ``right``: the node will be moved to the right of the ``target``
              node
            - ``last-sibling``: the node will be the new rightmost sibling of
              the ``target`` node
            - ``sorted-sibling``: the new node will be moved as a sibling of
              the ``target`` node according to the value of
              :attr:`node_order_by`

            .. note::

               If no ``pos`` is given the library will use ``last-sibling``,
               or ``sorted-sibling`` if :attr:`node_order_by` is enabled.

        :returns: None

        :raise InvalidPosition: when passing an invalid ``pos`` parm
        :raise InvalidPosition: when :attr:`node_order_by` is enabled and the
           ``pos`` parm wasn't ``sorted-sibling`` or ``sorted-child``
        :raise InvalidMoveToDescendant: when trying to move a node to one of
           it's own descendants
        :raise PathOverflow: when the library can't make room for the
           node's new position
        :raise MissingNodeOrderBy: when passing ``sorted-sibling`` or
           ``sorted-child`` as ``pos`` and the :attr:`node_order_by`
           attribute is missing
        """
        raise NotImplementedError

    def get_root_nodes(self):  # pragma: no cover
        """:returns: A queryset containing the root nodes in the tree."""
        raise NotImplementedError

    def get_first_root_node(self):
        """
        :returns:

            The first root node in the tree or ``None`` if it is empty.
        """
        return self.get_root_nodes().first()

    def get_last_root_node(self):
        """
        :returns:

            The last root node in the tree or ``None`` if it is empty.
        """
        return self.get_root_nodes().last()

    def get_descendants_group_count(self, parent=None):
        """
        Helper for a very common case: get a group of siblings and the number
        of *descendants* (not only children) in every sibling.

        :param parent:

            The parent of the siblings to return. If no parent is given, the
            root nodes will be returned.

        :returns:

            A `list` (**NOT** a Queryset) of node objects with an extra
            attribute: `descendants_count`.
        """
        if parent is None:
            qset = self.get_root_nodes()
        else:
            qset = self.get_children(parent)
        nodes = list(qset)
        for node in nodes:
            node.descendants_count = self.get_descendant_count(node)
        return nodes

    def get_annotated_list_qs(self, qs):
        """
        Efficiently generates an annotated list from a queryset.

        The queryset MUST be ordered by path, otherwise it will yield
        incorrect results. The queryset must also represent the entirety of
        a branch of a tree: excluded objects will not be fetched and will
        result in gaps in the tree.
        """
        result, info = [], {}
        start_depth, prev_depth = (None, None)
        for node in qs:
            depth = node.get_depth()
            if start_depth is None:
                start_depth = depth
            open = depth and (prev_depth is None or depth > prev_depth)
            if prev_depth is not None and depth < prev_depth:
                info["close"] = list(range(0, prev_depth - depth))
            info = {"open": open, "close": [], "level": depth - start_depth}
            result.append(
                (
                    node,
                    info,
                )
            )
            prev_depth = depth
        if start_depth and start_depth > 0:
            info["close"] = list(range(0, prev_depth - start_depth + 1))
        return result

    def get_annotated_list(self, parent=None, max_depth=None):
        """
        Gets an annotated list from a tree branch.

        :param parent:

            The node whose descendants will be annotated. The node itself
            will be included in the list. If not given, the entire tree
            will be annotated.

        :param max_depth:

            Optionally limit to specified depth
        """
        qs = self.get_tree(parent)
        if max_depth:
            qs = qs.filter(depth__lte=max_depth)
        return self.get_annotated_list_qs(qs)

    def get_visible_tree(self, allowed, *, on_hidden="prune", max_depth=None):
        """
        Return a complete, read-only projection of a tree.

        ``allowed`` may be a queryset or an iterable of primary keys. Every
        primary key in the result is guaranteed to be in that set.

        ``on_hidden`` selects the projection semantics:

        * ``"prune"``: hide a disallowed node and all of its descendants.
        * ``"promote"``: promote allowed descendants to their nearest allowed
          ancestor.

        ``max_depth`` is measured from the projected roots, starting at 1. It
        defines a presentation boundary, not an authorization rule.

        A sliced queryset cannot identify a complete tree and is rejected.

        The returned mapping contains:

        * ``roots``: projected root identifiers in DFS order.
        * ``nodes``: serializable node records in projected DFS order.
        * ``max_depth``: the requested projected depth limit, if any.
        * ``max_visible_depth``: the greatest projected depth in the result.
        * ``truncated``: whether one or more allowed branches end at
          ``max_depth``.
        * ``complete``: whether the input represents a non-paginated tree.

        Each node record contains ``id``, ``parent``, ``depth``, ``numchild``,
        ``open``, ``close``, ``truncated`` and ``fields``. ``open`` is true when
        the node has visible children; ``close`` is the number of projected
        levels that close before the next rendered node or at the end.
        """
        if on_hidden not in {"prune", "promote"}:
            raise ValueError("on_hidden must be either 'prune' or 'promote'.")
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be greater than or equal to 1.")

        using = self._db
        if isinstance(allowed, models.QuerySet):
            if allowed.model is not self.model:
                raise ValueError("allowed must be a queryset for this manager's model.")
            if allowed.query.is_sliced:
                raise ValueError("A sliced queryset does not represent a complete visible tree.")
            using = allowed.db
            allowed_qs = allowed.using(using)
            if allowed_qs.query.is_empty():
                return {
                    "roots": [],
                    "nodes": [],
                    "max_depth": max_depth,
                    "max_visible_depth": 0,
                    "truncated": False,
                    "complete": True,
                }
        elif isinstance(allowed, Iterable) and not isinstance(allowed, (str, bytes)):
            allowed_pks = list(allowed)
            if not allowed_pks:
                return {
                    "roots": [],
                    "nodes": [],
                    "max_depth": max_depth,
                    "max_visible_depth": 0,
                    "truncated": False,
                    "complete": True,
                }
            allowed_qs = self.using(using).filter(pk__in=allowed_pks)
        else:
            raise TypeError("allowed must be a QuerySet or an iterable of primary keys.")

        context = {"depth_by_pk": {}}
        nodes = list(self._get_visible_tree_nodes(allowed_qs, context))
        if not nodes:
            return {
                "roots": [],
                "nodes": [],
                "max_depth": max_depth,
                "max_visible_depth": 0,
                "truncated": False,
                "complete": True,
            }

        allowed_pks = {node.pk for node in nodes if getattr(node, "treebeard_visible", False)}
        projected = []
        records_by_pk = {}
        visible_stack = []
        skip_hidden_subtree = False
        skip_depth = None
        truncated_pks = set()

        for node in nodes:
            original_depth = self._get_visible_tree_node_depth(node, context)
            visible_stack = [item for item in visible_stack if item["depth"] < original_depth]

            if skip_hidden_subtree:
                if original_depth > skip_depth:
                    continue
                skip_hidden_subtree = False
                skip_depth = None

            if node.pk not in allowed_pks:
                if on_hidden == "prune":
                    skip_hidden_subtree = True
                    skip_depth = original_depth
                continue

            projected_parent = visible_stack[-1] if visible_stack else None
            projected_depth = projected_parent["projected_depth"] + 1 if projected_parent else 1

            if max_depth is not None and projected_depth > max_depth:
                if projected_parent:
                    truncated_pks.add(projected_parent["pk"])
                continue

            record = {
                "id": self._serialize_projected_pk(node),
                "parent": projected_parent["id"] if projected_parent else None,
                "depth": projected_depth,
                "numchild": 0,
                "open": False,
                "close": 0,
                "truncated": False,
                "fields": self._serialize_projected_fields(node),
            }
            projected.append(record)
            records_by_pk[node.pk] = record

            stack_item = {
                "pk": node.pk,
                "depth": original_depth,
                "projected_depth": projected_depth,
                "id": record["id"],
            }
            visible_stack.append(stack_item)

        if not projected:
            return {
                "roots": [],
                "nodes": [],
                "max_depth": max_depth,
                "max_visible_depth": 0,
                "truncated": False,
                "complete": True,
            }

        child_counts = {record["id"]: 0 for record in projected}
        for record in projected:
            if record["parent"] is not None:
                child_counts[record["parent"]] += 1
        for record in projected:
            record["numchild"] = child_counts[record["id"]]
            record["truncated"] = record["id"] in {records_by_pk[pk]["id"] for pk in truncated_pks}
            record["open"] = record["numchild"] > 0

        close_counts = self._projection_close_counts(projected)
        for record in projected:
            record["close"] = close_counts[record["id"]]

        roots = [record["id"] for record in projected if record["parent"] is None]
        return {
            "roots": roots,
            "nodes": projected,
            "max_depth": max_depth,
            "max_visible_depth": max((record["depth"] for record in projected), default=0),
            "truncated": bool(truncated_pks),
            "complete": True,
        }

    def _projection_close_counts(self, records):
        close_counts = {}
        for current, following in zip(records, records[1:]):
            close_counts[current["id"]] = max(0, current["depth"] - following["depth"])
        close_counts[records[-1]["id"]] = records[-1]["depth"]
        return close_counts

    def _serialize_projected_pk(self, node):
        pk_field = node._meta.pk
        value = pk_field.value_from_object(node)
        return value if value is None or is_protected_type(value) else pk_field.value_to_string(node)

    def _serialize_projected_fields(self, node):
        excluded = {"path", "depth", "numchild", "lft", "rgt", "tree_id", "parent", "sib_order"}
        protected_as_string = {
            "DateField",
            "DateTimeField",
            "TimeField",
            "DurationField",
            "DecimalField",
        }
        data = {}
        for field in node._meta.concrete_fields:
            if field.primary_key or field.name in excluded:
                continue
            if isinstance(field, models.ForeignKey):
                value = getattr(node, field.attname)
                data[field.name] = value if value is None or is_protected_type(value) else str(value)
            elif field.get_internal_type() == "JSONField":
                data[field.name] = field.value_from_object(node)
            else:
                value = field.value_from_object(node)
                data[field.name] = (
                    field.value_to_string(node)
                    if value is not None
                    and (not is_protected_type(value) or field.get_internal_type() in protected_as_string)
                    else value
                )
        return data

    def _get_visible_tree_node_depth(self, node, context):
        return node.get_depth()

    def _get_visible_tree_nodes(self, allowed_qs, context):  # pragma: no cover
        raise NotImplementedError

    @transaction.atomic
    def load_bulk(self, bulk_data, parent=None, keep_ids=False):
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

        :returns: A list of the added node PKs.
        """

        # tree, iterative preorder
        added = []
        bulk_data = prepare_dumpdata_for_loading(self.model, data=bulk_data, keep_ids=keep_ids)
        # stack of nodes to analyze
        stack = [(parent, deserialized_obj) for deserialized_obj in bulk_data[::-1]]

        while stack:
            parent, deserialized_obj = stack.pop()
            node_obj = deserialized_obj.object = (
                self.add_child(parent, instance=deserialized_obj.object)
                if parent
                else self.add_root(instance=deserialized_obj.object)
            )
            save_m2m(node_obj, deserialized_obj)
            added.append(node_obj.pk)
            # extending the stack with the current node as the parent of the new nodes
            stack.extend([(node_obj, child) for child in deserialized_obj.children[::-1]])
        return added

    def dump_bulk(self, parent=None, keep_ids=True):  # pragma: no cover
        """
        Dumps a tree branch to a python data structure.

        :param parent:

            The node whose descendants will be dumped. The node itself will be
            included in the dump. If not given, the entire tree will be dumped.

        :param keep_ids:

            Stores the pk value (primary key) of every node. Enabled by
            default.

        :returns: A python data structure, described with detail in
                  :meth:`load_bulk`
        """
        raise NotImplementedError

    def get_children(self, node):  # pragma: no cover
        """:returns: A queryset of all the node's children"""
        raise NotImplementedError

    def get_children_count(self, node):
        """:returns: The number of the node's children"""
        return self.get_children(node).count()

    def get_siblings(self, node):  # pragma: no cover
        """
        :returns:

            A queryset of all the node's siblings, including the node
            itself.
        """
        raise NotImplementedError

    def get_descendants(self, node, include_self=False, max_depth: int | None = None):
        """
        :returns:

            A queryset of all the node's descendants, doesn't
            include the node itself if include_self is False.

            If max_depth is set then the tree is limited to the specified depth relative
            to the node.
        """
        raise NotImplementedError

    def get_descendant_count(self, node, max_depth: int | None = None):
        """:returns: the number of descendants of a node

        If max_depth is set then the count is limited to the specified depth relative
        to the node.
        """
        return self.get_descendants(node, max_depth=max_depth).count()

    def get_first_child(self, node):
        """
        :returns:

            The node's leftmost child, or None if it has no children.
        """
        return self.get_children(node).first()

    def get_last_child(self, node):
        """
        :returns:

            The node's rightmost child, or None if it has no children.
        """
        return self.get_children(node).last()

    def get_first_sibling(self, node):
        """
        :returns:

            The node's leftmost sibling. Can return the node itself if
            it was the leftmost sibling.
        """
        return self.get_siblings(node).first()

    def get_last_sibling(self, node):
        """
        :returns:

            The rightmost node's sibling, can return the node itself if
            it was the rightmost sibling.
        """
        return self.get_siblings(node).last()

    def get_prev_sibling(self, node):
        """
        :returns:

            The node's previous sibling, or None if it was the leftmost
            sibling.
        """
        ids = list(self.get_siblings(node).values_list("pk", flat=True))
        idx = ids.index(node.pk)
        if idx > 0:
            return self.get_siblings(node).get(pk=ids[idx - 1])

    def get_next_sibling(self, node):
        """
        :returns:

            The next node's sibling, or None if it was the rightmost
            sibling.
        """
        ids = list(self.get_siblings(node).values_list("pk", flat=True))
        idx = ids.index(node.pk)
        if idx < len(ids) - 1:
            return self.get_siblings(node).get(pk=ids[idx + 1])

    def get_root(self, node):  # pragma: no cover
        """:returns: the root node for supplied node object."""
        raise NotImplementedError

    def get_parent(self, node, update=False):  # pragma: no cover
        """
        :returns: the parent node of the supplied node object.
            Caches the result in the object itself to help in loops.

        :param update: Updates the cached value.
        """
        raise NotImplementedError

    def get_ancestors(self, node):  # pragma: no cover
        """
        :returns:

            A queryset containing the node's ancestors,
            starting by the root node and descending to the parent.
            (some subclasses may return a list)
        """
        raise NotImplementedError

    def _prepare_pos_var(self, pos, method_name, valid_pos, valid_sorted_pos):
        if pos is None:
            if self.model.node_order_by:
                pos = "sorted-sibling"
            else:
                pos = "last-sibling"
        if pos not in valid_pos:
            raise InvalidPosition(f"Invalid relative position: {pos}")
        if self.model.node_order_by and pos not in valid_sorted_pos:
            raise InvalidPosition(
                f"Must use {' or '.join(valid_sorted_pos)} in {method_name} when node_order_by is enabled"
            )
        if pos in valid_sorted_pos and not self.model.node_order_by:
            raise MissingNodeOrderBy("Missing node_order_by attribute.")
        return pos

    _valid_pos_for_add_sibling = ("first-sibling", "left", "right", "last-sibling", "sorted-sibling")
    _valid_pos_for_sorted_add_sibling = ("sorted-sibling",)

    def _prepare_pos_var_for_add_sibling(self, pos):
        return self._prepare_pos_var(
            pos, "add_sibling", self._valid_pos_for_add_sibling, self._valid_pos_for_sorted_add_sibling
        )

    _valid_pos_for_move = _valid_pos_for_add_sibling + ("first-child", "last-child", "sorted-child")
    _valid_pos_for_sorted_move = _valid_pos_for_sorted_add_sibling + ("sorted-child",)

    def _prepare_pos_var_for_move(self, pos):
        return self._prepare_pos_var(pos, "move", self._valid_pos_for_move, self._valid_pos_for_sorted_move)

    def get_sorted_pos_queryset(self, siblings, newobj):
        """
        :returns:

            A queryset of the nodes that must be moved to the right.
            Called only for Node models with :attr:`node_order_by`

        This function is based on _insertion_target_filters from django-mptt
        (MIT licensed) by Jonathan Buchanan:
        https://github.com/django-mptt/django-mptt/blob/0.3.0/mptt/signals.py

        See LICENSE-THIRD-PARTY
        """

        fields, filters = [], []
        for field in self.model.node_order_by:
            comparator = "gt"
            if field.startswith("-"):
                field = field[1:]
                comparator = "lt"

            value = getattr(newobj, field)
            if value is None:
                warnings.warn(
                    f"Received a null value for field '{field}', which is used "
                    f"by '{self.model.__name__}.node_order_by'. "
                    "This field will be ignored when sorting the object.",
                    category=RuntimeWarning,
                )
                continue

            filters.append(Q(*[Q(**{f: v}) for f, v in fields] + [Q(**{f"{field}__{comparator}": value})]))
            fields.append((field, value))

        if not filters:
            return siblings

        return siblings.filter(reduce(operator.or_, filters))
