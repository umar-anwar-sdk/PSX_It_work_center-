from django.contrib import admin
from .models import PDFDocument,ExtractedCompanyRecord,ComparisonResult

# Register your models here.

admin.site.register(PDFDocument)
admin.site.register(ExtractedCompanyRecord)
admin.site.register(ComparisonResult)