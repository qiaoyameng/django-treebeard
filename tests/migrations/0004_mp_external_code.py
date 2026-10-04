from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tests", "0003_mp_external_short_path")]

    operations = [
        migrations.CreateModel(
            name="MP_TestNodeExternalCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=255, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("external_code", models.CharField(max_length=64, unique=True)),
                ("desc", models.CharField(max_length=255, blank=True)),
            ],
        ),
    ]
