from django.db import models


class PDFDocument(models.Model):
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to="pdfs/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    report_date = models.DateField(null=True, blank=True)
    report_time = models.TimeField(null=True, blank=True)

    is_processed = models.BooleanField(default=False)
    processing_error = models.TextField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    file_hash = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["report_date", "report_time"])]

    def __str__(self):
        return self.name


class ExtractedCompanyRecord(models.Model):
    pdf_document = models.ForeignKey(
        PDFDocument,
        on_delete=models.CASCADE,
        related_name="extracted_companies",
    )
    company_name = models.CharField(max_length=255)
    symbol = models.CharField(max_length=50, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_percent = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    volume = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_name"]

    def __str__(self):
        return self.company_name


class ComparisonResult(models.Model):
    previous_pdf = models.ForeignKey(
        PDFDocument,
        on_delete=models.CASCADE,
        related_name="previous_comparisons",
        null=True,
        blank=True,
    )

    current_pdf = models.ForeignKey(
        PDFDocument,
        on_delete=models.CASCADE,
        related_name="current_comparisons",
    )

    symbol = models.CharField(max_length=50)
    company_name = models.CharField(max_length=255)

    status = models.CharField(
        max_length=20,
        choices=[
            ("NEW", "NEW"),
            ("REMOVED", "REMOVED"),
            ("EXISTING", "EXISTING"),
        ],
    )

    previous_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    current_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.symbol} - {self.status}"