from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tests", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MP_TestNodeBilling",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=255, unique=True)),
                ("depth", models.PositiveIntegerField()),
                ("numchild", models.PositiveIntegerField(default=0)),
                ("desc", models.CharField(max_length=255, blank=True)),
                ("billing_rate", models.CharField(max_length=32, null=True)),
                ("billing_defined", models.BooleanField(default=False)),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
