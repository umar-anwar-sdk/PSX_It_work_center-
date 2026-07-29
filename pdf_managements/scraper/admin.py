from django.contrib import admin
<<<<<<< Updated upstream
from .models import PDFDocument,ExtractedCompanyRecord,ComparisonResult

# Register your models here.

admin.site.register(PDFDocument)
admin.site.register(ExtractedCompanyRecord)
admin.site.register(ComparisonResult)
=======
from .models import PDFDocument, ExtractedCompanyRecord

# Register your models here.


admin.site.register(PDFDocument)
admin.site.register(ExtractedCompanyRecord)
>>>>>>> Stashed changes
