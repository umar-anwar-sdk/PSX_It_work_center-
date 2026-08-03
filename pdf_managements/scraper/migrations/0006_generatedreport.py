from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0005_alter_comparisonresult_current_pdf_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="GeneratedReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("report_type", models.CharField(choices=[("daily", "Daily Report"), ("weekly", "Weekly Report"), ("monthly", "Monthly Report"), ("quarterly", "Quarterly Report")], max_length=20)),
                ("name", models.CharField(max_length=255)),
                ("date_from", models.DateField()),
                ("date_to", models.DateField()),
                ("file", models.FileField(upload_to="reports/")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
