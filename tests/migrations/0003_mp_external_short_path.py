from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tests", "0002_mp_external_nodes")]

    operations = [
        migrations.CreateModel(
            name="MP_TestNodeExternalShortPath",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=4, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("external_id", models.CharField(max_length=64, unique=True)),
            ],
        ),
    ]
