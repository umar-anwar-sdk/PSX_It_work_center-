from django.db import models

# Create your models here.
from django.db import models


class PDFDocument(models.Model):
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to='pdfs/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    report_date = models.DateField(null=True, blank=True)
    report_time = models.TimeField(null=True, blank=True)

    is_processed = models.BooleanField(default=False)
    processing_error = models.TextField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    file_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['report_date', 'report_time'])]

    def __str__(self):
        return self.name


class ParsedTableData(models.Model):
    pdf_document = models.ForeignKey(
        PDFDocument,
        on_delete=models.CASCADE,
        related_name='table_data'
    )

    table_index = models.IntegerField()
    row_index = models.IntegerField()

    row_data = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('pdf_document', 'table_index', 'row_index')
        ordering = ['table_index', 'row_index']

    def __str__(self):
        return f"Table {self.table_index} Row {self.row_index}"
