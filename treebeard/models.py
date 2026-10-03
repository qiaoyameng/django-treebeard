"""Models and base API"""

import copy
import operator
import warnings
from contextlib import suppress
from functools import reduce

from django.core import checks
from django.db import models, router, transaction
from django.db.models import Q
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

    def copy_subtree(
        self,
        node,
        target=None,
        pos="last-child",
        *,
        fields=None,
        ignore_fields=(),
        field_callbacks=None,
        unique_callbacks=None,
        external_references="reject",
        copy_m2m=False,
        using=None,
    ):
        """
        Copy a node and all descendants.

        Return a dictionary mapping original primary keys to the primary keys
        of their copies.
        """
        using = using or router.db_for_write(self.tree_model, instance=node)
        with transaction.atomic(using=using):
            return self._copy_subtree(
                node,
                target,
                pos,
                fields=fields,
                ignore_fields=ignore_fields,
                field_callbacks=field_callbacks,
                unique_callbacks=unique_callbacks,
                external_references=external_references,
                copy_m2m=copy_m2m,
                using=using,
            )

    def _copy_subtree(
        self,
        node,
        target=None,
        pos="last-child",
        *,
        fields=None,
        ignore_fields=(),
        field_callbacks=None,
        unique_callbacks=None,
        external_references="reject",
        copy_m2m=False,
        using=None,
    ):
        external_references = {
            "reject": "reject",
            "raise": "reject",
            "error": "reject",
            "preserve": "preserve",
            "keep": "preserve",
        }.get(external_references)
        if external_references is None:
            raise ValueError("external_references must be either 'reject' or 'preserve'.")

        if isinstance(fields, str) or not hasattr(fields, "items"):
            copy_fields = (fields,) if isinstance(fields, str) else tuple(fields or ())
            field_actions = {}
        else:
            copy_fields = ()
            field_actions = {}
            for field_name, action in fields.items():
                if callable(action):
                    field_actions[field_name] = "recompute"
                    field_callbacks = dict(field_callbacks or {})
                    field_callbacks[field_name] = action
                else:
                    field_actions[field_name] = action

        ignore_fields = tuple(ignore_fields)
        field_callbacks = dict(field_callbacks or {})
        unique_callbacks = dict(unique_callbacks or {})

        valid_actions = {"copy", "ignore", "recompute"}
        for field_name, action in field_actions.items():
            if action not in valid_actions:
                raise ValueError(f"Unsupported copy action {action!r} for field {field_name!r}.")
        for field_name in field_callbacks:
            if not callable(field_callbacks[field_name]):
                raise ValueError(f"The callback for field {field_name!r} must be callable.")
        for field_name, callback in unique_callbacks.items():
            if not callable(callback):
                raise ValueError(f"The callback for unique field {field_name!r} must be callable.")

        if isinstance(copy_m2m, str):
            m2m_fields = {copy_m2m}
        elif copy_m2m is True:
            m2m_fields = None
        elif copy_m2m is False:
            m2m_fields = set()
        else:
            m2m_fields = set(copy_m2m)

        tree_model = self.tree_model
        mgr = self.db_manager(using)
        sources = list(mgr.get_tree(node))
        subtree_pks = {source.pk for source in sources}
        internal_concrete_fields = {
            source.__class__: tuple(
                field
                for field in source.__class__._meta.concrete_fields
                if field.many_to_one and not field.remote_field.parent_link
            )
            for source in sources
        }
        source_refresh_fields = {
            source.__class__: {
                field.attname
                for field in internal_concrete_fields.get(source.__class__, ())
                if field.related_model in tree_model._meta.get_parent_list() + [tree_model]
            }
            for source in sources
        }
        for model_class, refresh_fields in source_refresh_fields.items():
            if refresh_fields:
                refreshed_sources = model_class._base_manager.using(using).filter(pk__in=subtree_pks).values(
                    "pk", *refresh_fields
                )
                refreshed_values = {row["pk"]: row for row in refreshed_sources}
                for source in sources:
                    if source.__class__ == model_class:
                        for field_name in refresh_fields:
                            setattr(source, field_name, refreshed_values[source.pk][field_name])
        source_by_pk = {source.pk: source for source in sources}

        if target is not None and not isinstance(target, tree_model):
            raise ValueError("The copy target must be a node of the same tree model.")

        if target is not None and target._state.adding:
            raise ValueError("The copy target must be saved before copying a subtree.")

        if target is not None and target.pk in subtree_pks:
            raise InvalidPosition("A subtree cannot be copied into itself.")

        if target is None:
            if pos not in ("first-sibling", "last-sibling", "sorted-sibling", None):
                raise InvalidPosition("Root nodes can only be copied using a sibling position.")
            pos = "sorted-sibling" if tree_model.node_order_by and pos is None else pos or "last-sibling"
            if pos not in self._valid_pos_for_sorted_add_sibling and tree_model.node_order_by:
                raise InvalidPosition("Must use sorted-sibling when node_order_by is enabled")
            if pos in self._valid_pos_for_sorted_add_sibling and not tree_model.node_order_by:
                raise MissingNodeOrderBy("Missing node_order_by attribute.")
        else:
            if pos is None:
                pos = "sorted-child" if tree_model.node_order_by else "last-child"
            self._prepare_pos_var_for_move(pos)

        def tree_relation(field):
            related_model = field.related_model
            return (
                field.remote_field is not None
                and related_model is not None
                and tree_model in related_model._meta.get_parent_list() + [related_model]
            )

        def structure_field(field):
            return field.name in tree_model.MOVENODE_FORM_EXCLUDED_FIELDS or (
                field.one_to_one
                and field.remote_field.parent_link
                and field.related_model in tree_model._meta.get_parent_list()
            )

        def field_policy(field):
            if field.name in ignore_fields:
                return "ignore"
            if field.name in field_callbacks:
                return "recompute"
            if field.name in field_actions:
                return field_actions[field.name]
            if copy_fields and field.name not in copy_fields:
                return "ignore"
            return "copy"

        clones = {}
        original_parent_pks = {}
        for source in sources:
            clone = copy.copy(source)
            clone.pk = None
            clone.id = None
            clone._state.adding = True
            clone._state.db = using
            clone._state.fields_cache = {}

            for field in source.__class__._meta.concrete_fields:
                if structure_field(field):
                    setattr(clone, field.attname, field.get_default())

            if source.parent_id is not None if hasattr(source, "parent_id") else False:
                original_parent_pks[source.pk] = source.parent_id
            elif source.depth > 1 if hasattr(source, "depth") else False:
                original_parent_pks[source.pk] = mgr.get_parent(source).pk

            for field in source.__class__._meta.concrete_fields:
                if structure_field(field) or field.primary_key or field.many_to_many:
                    continue

                policy = field_policy(field)
                if policy == "ignore":
                    setattr(clone, field.attname, field.get_default())
                elif policy == "recompute":
                    if field.name not in field_callbacks:
                        raise ValueError(f"Missing callback for field {field.name!r}.")
                    setattr(clone, field.attname, field_callbacks[field.name](source))

                if field.unique and not tree_relation(field):
                    callback = unique_callbacks.get(field.name) or field_callbacks.get(field.name)
                    if callback is None:
                        raise ValueError(f"Missing callback for unique field {field.name!r}.")
                    setattr(clone, field.attname, callback(source))

                field_value = getattr(source, field.attname)
                if tree_relation(field) and field.many_to_one and not field.remote_field.parent_link:
                    if field_value in subtree_pks:
                        setattr(clone, field.attname, None)
                    elif field_value is not None and external_references != "preserve":
                        raise ValueError(
                            f"Cannot copy external reference {field.name!r} without an explicit policy."
                        )
                elif policy == "copy" and field.many_to_one:
                    if (
                        tree_relation(field)
                        and field_value is not None
                        and field_value not in subtree_pks
                        and external_references == "reject"
                    ):
                        raise ValueError(f"Cannot copy external reference {field.name!r} without an explicit policy.")

            if hasattr(clone, "numchild"):
                clone.numchild = 0

            with suppress(AttributeError):
                del clone._cached_parent_obj
            with suppress(AttributeError):
                del clone._cached_depth

            clones[source.pk] = clone

        original_parent_pks.pop(node.pk, None)

        m2m_sources = {}
        custom_through_rows = {}
        for source in sources:
            for field in source.__class__._meta.many_to_many:
                if m2m_fields is not None and field.name not in m2m_fields:
                    continue

                rel_field = field.remote_field
                if rel_field.through._meta.auto_created:
                    m2m_sources[(source.pk, field.name)] = list(
                        getattr(source, field.name).using(using).values_list("pk", flat=True)
                    )
                else:
                    custom_through_rows.setdefault(field, []).extend(
                        list(
                            rel_field.through._base_manager.using(using).filter(
                                **{field.m2m_field_name(): source.pk}
                            )
                        )
                    )

        for rows in custom_through_rows.values():
            for row in rows:
                for field in row.__class__._meta.local_concrete_fields:
                    if not field.many_to_one or field.primary_key:
                        continue
                    value = getattr(row, field.attname)
                    if (
                        value is not None
                        and field.related_model in tree_model._meta.get_parent_list() + [tree_model]
                        and value not in subtree_pks
                        and external_references == "reject"
                    ):
                        raise ValueError("Cannot copy an external subtree reference without an explicit policy.")

        def insert_clone(source, clone):
            source_parent_pk = original_parent_pks.get(source.pk)
            if source_parent_pk is None:
                if target is None:
                    if pos == "sorted-sibling":
                        last_root = mgr.get_last_root_node()
                        if last_root:
                            return mgr.add_sibling(last_root, "sorted-sibling", instance=clone)
                        return mgr.add_root(instance=clone)
                    if pos == "first-sibling":
                        first_root = mgr.get_first_root_node()
                        if first_root:
                            return mgr.add_sibling(first_root, "first-sibling", instance=clone)
                        return mgr.add_root(instance=clone)
                    return mgr.add_root(instance=clone)

                if pos == "first-child":
                    first_child = mgr.get_first_child(target)
                    if first_child is None:
                        return mgr.add_child(target, instance=clone)
                    return mgr.add_sibling(first_child, "first-sibling", instance=clone)
                if pos in ("last-child", "sorted-child"):
                    return mgr.add_child(target, instance=clone)
                return mgr.add_sibling(target, pos, instance=clone)

            parent = clones[source_parent_pk]
            return mgr.add_child(parent, instance=clone)

        pk_map = {}
        for source in sources:
            new_node = insert_clone(source, clones[source.pk])
            pk_map[source.pk] = new_node.pk

        if target is not None:
            target.refresh_from_db(using=using)

        internal_fields = set()
        for source in sources:
            for field in source.__class__._meta.concrete_fields:
                if (
                    field.many_to_one
                    and not field.remote_field.parent_link
                    and not structure_field(field)
                    and tree_relation(field)
                ):
                    internal_fields.add((source.__class__, field))

        for model, field in internal_fields:
            updates = []
            for source in sources:
                old_value = getattr(source, field.attname)
                if old_value in pk_map:
                    updates.append((pk_map[source.pk], pk_map[old_value]))
            for new_pk, new_value in updates:
                model._base_manager.using(using).filter(pk=new_pk).update(**{field.attname: new_value})

        for (source_pk, field_name), target_pks in m2m_sources.items():
            field = source_by_pk[source_pk].__class__._meta.get_field(field_name)
            new_node = tree_model._base_manager.using(using).get(pk=pk_map[source_pk])
            mapped_pks = []
            for target_pk in target_pks:
                if target_pk in pk_map:
                    mapped_pks.append(pk_map[target_pk])
                elif field.related_model not in tree_model._meta.get_parent_list() + [tree_model]:
                    mapped_pks.append(target_pk)
                elif external_references == "preserve":
                    mapped_pks.append(target_pk)
                else:
                    raise ValueError("Cannot copy an external subtree reference without an explicit policy.")
            getattr(new_node, field.name).using(using).add(*mapped_pks)

        for field, rows in custom_through_rows.items():
            through_model = field.remote_field.through
            source_name = field.m2m_field_name()
            target_name = field.m2m_reverse_field_name()
            new_rows = []
            seen = set()

            for row in rows:
                source_pk = getattr(row, f"{source_name}_id")
                target_pk = getattr(row, f"{target_name}_id")
                new_source_pk = pk_map.get(source_pk)
                new_target_pk = pk_map.get(target_pk)
                source_field = through_model._meta.get_field(source_name)
                target_field = through_model._meta.get_field(target_name)
                tree_models = tree_model._meta.get_parent_list() + [tree_model]
                if new_source_pk is None:
                    if source_field.related_model in tree_models and external_references != "preserve":
                        raise ValueError("Cannot copy an external subtree reference without an explicit policy.")
                    new_source_pk = source_pk
                if new_target_pk is None:
                    if target_field.related_model in tree_models and external_references != "preserve":
                        raise ValueError("Cannot copy an external subtree reference without an explicit policy.")
                    new_target_pk = target_pk

                key = [new_source_pk, new_target_pk]
                new_row = through_model()
                setattr(new_row, f"{source_name}_id", new_source_pk)
                setattr(new_row, f"{target_name}_id", new_target_pk)

                for through_field in through_model._meta.local_concrete_fields:
                    if through_field.primary_key or through_field.name in (source_name, target_name):
                        continue
                    value = getattr(row, through_field.attname)
                    if through_field.many_to_one:
                        if value in pk_map:
                            value = pk_map[value]
                        elif (
                            value is not None
                            and through_field.related_model in tree_models
                            and external_references != "preserve"
                        ):
                            raise ValueError("Cannot copy an external subtree reference without an explicit policy.")
                    setattr(new_row, through_field.attname, value)
                    if not through_field.many_to_one:
                        key.append((through_field.name, value))

                key = tuple(key)
                if key not in seen:
                    seen.add(key)
                    new_rows.append(new_row)

            through_model._base_manager.using(using).bulk_create(
                new_rows, ignore_conflicts=through_model._meta.auto_created
            )

        return pk_map

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
