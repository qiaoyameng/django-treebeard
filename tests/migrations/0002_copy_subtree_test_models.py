import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tests", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="AL_TestNodeCopySubtree",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sib_order", models.PositiveIntegerField()),
                ("desc", models.CharField(max_length=255)),
                ("code", models.CharField(max_length=255, unique=True)),
                ("score", models.IntegerField(default=0)),
                (
                    "link",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="tests.al_testnodecopysubtree",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children_set",
                        to="tests.al_testnodecopysubtree",
                    ),
                ),
                ("links", models.ManyToManyField(to="tests.al_testnodecopysubtree")),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="MP_TestNodeCopySubtree",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=255, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("desc", models.CharField(max_length=255)),
                ("code", models.CharField(max_length=255, unique=True)),
                ("score", models.IntegerField(default=0)),
                (
                    "link",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="tests.mp_testnodecopysubtree",
                    ),
                ),
                ("links", models.ManyToManyField(to="tests.mp_testnodecopysubtree")),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="NS_TestNodeCopySubtree",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lft", models.PositiveIntegerField(db_index=True)),
                ("rgt", models.PositiveIntegerField(db_index=True)),
                ("tree_id", models.PositiveIntegerField(db_index=True)),
                ("depth", models.PositiveIntegerField(db_index=True)),
                ("desc", models.CharField(max_length=255)),
                ("code", models.CharField(max_length=255, unique=True)),
                ("score", models.IntegerField(default=0)),
                (
                    "link",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="tests.ns_testnodecopysubtree",
                    ),
                ),
                ("links", models.ManyToManyField(to="tests.ns_testnodecopysubtree")),
            ],
            options={"abstract": False},
        ),
    ]
