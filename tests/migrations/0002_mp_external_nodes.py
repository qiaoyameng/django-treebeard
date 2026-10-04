from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tests", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="MP_TestNodeExternal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=255, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("external_id", models.CharField(max_length=64, unique=True)),
                ("desc", models.CharField(max_length=255, blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="MP_TestNodeExternalSorted",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=255, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("external_id", models.CharField(max_length=64, unique=True)),
                ("seq", models.IntegerField()),
            ],
        ),
    ]
