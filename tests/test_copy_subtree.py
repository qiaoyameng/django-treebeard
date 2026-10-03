import pytest
from django.db import IntegrityError

from tests import models


@pytest.fixture(params=models.COPY_MODELS)
def copy_fixture(request):
    model = request.param

    outside = model.objects.add_root({"desc": "outside", "note": "outside note", "code": "outside"})
    source = model.objects.add_root({"desc": "source", "note": "source note", "code": "source"})
    left = model.objects.add_child(source, {"desc": "left", "note": "left note", "code": "left"})
    right = model.objects.add_child(source, {"desc": "right", "note": "right note", "code": "right"})
    descendant = model.objects.add_child(left, {"desc": "descendant", "note": "descendant note", "code": "descendant"})
    external_ref = model.objects.add_child(
        source, {"desc": "external-ref", "note": "external note", "code": "external-ref"}
    )

    model.objects.filter(pk=left.pk).update(internal_ref_id=right.pk)
    model.objects.filter(pk=right.pk).update(internal_ref_id=left.pk)
    model.objects.filter(pk=source.pk).update(internal_ref_id=descendant.pk)
    model.objects.filter(pk=external_ref.pk).update(internal_ref_id=outside.pk)

    left.links.add(right)
    source.links.add(descendant)
    external_ref.links.add(outside)
    link_model = model._meta.get_field("links").remote_field.through
    link_model.objects.filter(source=left, target=right).update(position=10)

    return {
        "model": model,
        "outside": outside,
        "source": source,
        "left": left,
        "right": right,
        "descendant": descendant,
        "external_ref": external_ref,
        "link_model": link_model,
    }


def code_callback(node):
    return f"{node.code}-copy"


@pytest.mark.django_db
def test_copy_subtree_relinks_internal_references_and_copies_m2m(copy_fixture):
    model = copy_fixture["model"]
    original_nodes = {name: copy_fixture[name] for name in ("source", "left", "right", "descendant", "external_ref")}

    mapping = model.objects.copy_subtree(
        copy_fixture["source"],
        target=copy_fixture["outside"],
        pos="last-child",
        fields={"desc": "recompute"},
        ignore_fields=("note",),
        field_callbacks={"desc": lambda node: f"{node.desc} copied"},
        unique_callbacks={"code": code_callback},
        external_references="preserve",
        copy_m2m=True,
    )

    assert set(mapping) == {node.pk for node in original_nodes.values()}
    assert len(set(mapping.values())) == len(mapping)
    assert mapping.keys().isdisjoint(mapping.values())

    copied = {name: model.objects.get(pk=mapping[node.pk]) for name, node in original_nodes.items()}

    assert model.objects.get_parent(copied["source"]) == copy_fixture["outside"]
    assert copied["left"].desc == "left copied"
    assert copied["left"].note is None
    assert copied["left"].code == "left-copy"
    assert list(model.objects.get_children(copied["source"]).order_by("desc")) == [
        copied["external_ref"],
        copied["left"],
        copied["right"],
    ]

    assert copied["left"].internal_ref_id == mapping[copy_fixture["right"].pk]
    assert copied["right"].internal_ref_id == mapping[copy_fixture["left"].pk]
    assert copied["source"].internal_ref_id == mapping[copy_fixture["descendant"].pk]
    assert copied["external_ref"].internal_ref_id == copy_fixture["outside"].pk

    assert set(copied["left"].links.values_list("pk", flat=True)) == {mapping[copy_fixture["right"].pk]}
    assert set(copied["source"].links.values_list("pk", flat=True)) == {mapping[copy_fixture["descendant"].pk]}
    assert set(copied["external_ref"].links.values_list("pk", flat=True)) == {copy_fixture["outside"].pk}
    copied_link = copy_fixture["link_model"].objects.get(
        source_id=mapping[copy_fixture["left"].pk], target_id=mapping[copy_fixture["right"].pk]
    )
    assert copied_link.position == 10

    original_link_count = copy_fixture["link_model"].objects.filter(
        source_id__in=[node.pk for node in original_nodes.values()]
    ).count()
    copied_link_count = copy_fixture["link_model"].objects.filter(
        source_id__in=[new_pk for new_pk in mapping.values()]
    ).count()
    assert original_link_count == copied_link_count

    copied["left"].desc = "changed copy"
    copied["left"].save(update_fields=["desc"])
    model.objects.filter(pk=copied["left"].pk).update(internal_ref_id=None)
    copied["left"].links.clear()

    copy_fixture["left"].refresh_from_db()
    assert copy_fixture["left"].desc == "left"
    assert copy_fixture["left"].internal_ref_id == copy_fixture["right"].pk
    assert set(copy_fixture["left"].links.values_list("pk", flat=True)) == {copy_fixture["right"].pk}


@pytest.mark.django_db
def test_copy_subtree_can_insert_at_first_child(copy_fixture):
    model = copy_fixture["model"]
    existing_child = model.objects.add_child(
        copy_fixture["outside"], {"desc": "existing", "note": "", "code": "existing-child"}
    )

    mapping = model.objects.copy_subtree(
        copy_fixture["source"],
        target=copy_fixture["outside"],
        pos="first-child",
        ignore_fields=("note",),
        unique_callbacks={"code": code_callback},
        external_references="preserve",
    )

    copy_fixture["outside"].refresh_from_db()
    existing_child.refresh_from_db()
    copied_root = model.objects.get(pk=mapping[copy_fixture["source"].pk])
    children = list(model.objects.get_children(copy_fixture["outside"]))
    assert children[0] == copied_root
    assert children[-1] == existing_child


@pytest.mark.django_db
def test_copy_subtree_rejects_external_reference_by_default(copy_fixture):
    model = copy_fixture["model"]
    before_count = model.objects.count()

    with pytest.raises(ValueError, match="external reference"):
        model.objects.copy_subtree(
            copy_fixture["external_ref"],
            target=copy_fixture["source"],
            unique_callbacks={"code": code_callback},
        )

    assert model.objects.count() == before_count


@pytest.mark.django_db
def test_copy_subtree_preserves_external_reference_when_requested(copy_fixture):
    model = copy_fixture["model"]

    mapping = model.objects.copy_subtree(
        copy_fixture["external_ref"],
        target=copy_fixture["source"],
        ignore_fields=("note",),
        unique_callbacks={"code": code_callback},
        external_references="preserve",
        copy_m2m=True,
    )

    copied = model.objects.get(pk=mapping[copy_fixture["external_ref"].pk])
    assert copied.internal_ref_id == copy_fixture["outside"].pk
    assert set(copied.links.values_list("pk", flat=True)) == {copy_fixture["outside"].pk}


@pytest.mark.django_db
def test_copy_subtree_recomputes_fields_and_skips_m2m_by_default(copy_fixture):
    model = copy_fixture["model"]

    mapping = model.objects.copy_subtree(
        copy_fixture["left"],
        target=copy_fixture["source"],
        fields={"desc": lambda node: node.desc.upper(), "note": "ignore"},
        unique_callbacks={"code": code_callback},
        external_references="preserve",
    )

    copied = model.objects.get(pk=mapping[copy_fixture["left"].pk])
    assert copied.desc == "LEFT"
    assert copied.code == "left-copy"
    assert copied.note is None
    assert list(copied.links.values_list("pk", flat=True)) == []


@pytest.mark.django_db
def test_copy_subtree_rejects_unknown_external_reference_policy(copy_fixture):
    model = copy_fixture["model"]
    before_count = model.objects.count()

    with pytest.raises(ValueError, match="external_references"):
        model.objects.copy_subtree(
            copy_fixture["source"],
            target=copy_fixture["outside"],
            unique_callbacks={"code": code_callback},
            external_references="rewrite",
        )

    assert model.objects.count() == before_count


@pytest.mark.django_db
def test_copy_subtree_rolls_back_when_callback_fails(copy_fixture):
    model = copy_fixture["model"]
    before_count = model.objects.count()

    def failing_callback(node):
        if node.pk == copy_fixture["left"].pk:
            raise RuntimeError("callback failed")
        return code_callback(node)

    with pytest.raises(RuntimeError, match="callback failed"):
        model.objects.copy_subtree(
            copy_fixture["source"],
            target=copy_fixture["outside"],
            ignore_fields=("note",),
            unique_callbacks={"code": failing_callback},
            external_references="preserve",
        )

    assert model.objects.count() == before_count


@pytest.mark.django_db
def test_copy_subtree_rolls_back_when_constraint_fails(copy_fixture):
    model = copy_fixture["model"]
    before_count = model.objects.count()

    with pytest.raises(IntegrityError):
        model.objects.copy_subtree(
            copy_fixture["source"],
            target=copy_fixture["outside"],
            ignore_fields=("note",),
            unique_callbacks={"code": lambda node: "duplicate-code"},
            external_references="preserve",
        )

    assert model.objects.count() == before_count
