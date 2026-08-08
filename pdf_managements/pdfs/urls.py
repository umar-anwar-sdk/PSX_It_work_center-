from django.urls import path

from .views import (
    company_analysis,
    daily_market_explorer,
    download_report,
    home,
    market_analysis,
    market_comparison,
    pdf_details,
    delete_pdf,
    pdf_management,
    reports,
    search_screener,
    settings,
    watchlist,
)

urlpatterns = [
    path("", home, name="home"),
    path("pages/company-analysis/", company_analysis, name="company-analysis"),
    path("pages/daily-market-explorer/", daily_market_explorer, name="daily-market-explorer"),
    path("pages/market-analytics/", market_analysis, name="market-analytics"),
    path("pages/market-comparison/", market_comparison, name="market-comparison"),
    path("pages/pdf-management/", pdf_management, name="pdf-management"),
    path("pages/extracted-data/<int:pk>/", pdf_details, name="extracted-data"),
    path("pages/pdf-management/<int:pk>/delete/", delete_pdf, name="delete-pdf"),
    path("pages/reports/", reports, name="reports"),
    path("pages/reports/<int:pk>/download/", download_report, name="download-report"),
    path("pages/search-screener/", search_screener, name="search-screener"),
    path("pages/settings/", settings, name="settings"),
    path("pages/watchlist/", watchlist, name="watchlist"),
]
