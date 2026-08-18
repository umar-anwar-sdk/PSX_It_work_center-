from django.contrib import admin
from .models import ComparisonResult, ExtractedCompanyRecord, GeneratedReport, PDFDocument

# Register your models here.

admin.site.register(PDFDocument)
admin.site.register(ExtractedCompanyRecord)
admin.site.register(ComparisonResult)
admin.site.register(GeneratedReport)
