from django.contrib import admin
from .models import (
    AlertHistory,
    AlertSetting,
    ClientCompany,
    CompanySettings,
    ModulePermission,
    ScrapedRecord,
    WatchlistEntry,
)

# Register your models here.

admin.site.register(ScrapedRecord)
admin.site.register(ClientCompany)
admin.site.register(ModulePermission)
admin.site.register(CompanySettings)
admin.site.register(AlertSetting)
admin.site.register(AlertHistory)
admin.site.register(WatchlistEntry)
