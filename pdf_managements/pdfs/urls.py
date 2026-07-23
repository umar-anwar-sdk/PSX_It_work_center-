from .views import home, company_analysis, daily_market_explorer, market_analysis, market_comparison, pdf_management, reports, search_screener, settings, watchlist
from django.urls import path

urlpatterns = [
    path('', home, name='home'),
    path('pages/company-analysis/', company_analysis, name='company-analysis'),
    path('pages/daily-market-explorer/', daily_market_explorer, name='daily-market-explorer'),
    path('pages/market-analytics/', market_analysis, name='market-analytics'),
    path('pages/market-comparison/', market_comparison, name='market-comparison'),
    path('pages/pdf-management/', pdf_management, name='pdf-management'),
    path('pages/reports/', reports, name='reports'),
    path('pages/search-screener/', search_screener, name='search-screener'),
    path('pages/settings/', settings, name='settings'),
    path('pages/watchlist/', watchlist, name='watchlist'),
]
