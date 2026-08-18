import json
import logging
import os
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit
from django.utils import timezone

from scraper.forms import PDFDocumentForm
from scraper.models import ComparisonResult, ExtractedCompanyRecord, GeneratedReport, PDFDocument
from scraper.utils import get_file_hash, get_table_data, process_pdf_document

from .authz import admin_required, get_client_company, has_module_permission, module_permission_required
from .forms import (
    ClientCompanyCreateForm,
    ClientCompanyUpdateForm,
    ClientProfileUpdateForm,
    CompanySettingsForm,
)
from .models import (
    AlertHistory,
    AlertSetting,
    ClientCompany,
    CompanySettings,
    MODULE_CHOICES,
    ModulePermission,
    ScrapedRecord,
    WatchlistAlertRule,
    WatchlistEntry,
)

logger = logging.getLogger(__name__)


class CompanyCollection(list):
    def values_list(self, *fields, flat=False):
        if not fields:
            return []

        field_name = fields[0]
        if flat:
            return [getattr(item, field_name, None) for item in self]

        return [tuple(getattr(item, field_name, None) for field_name in fields) for item in self]


def _as_company_row(record, status=""):
    if isinstance(record, dict):
        payload = dict(record)
    else:
        payload = {
            "company_name": getattr(record, "company_name", ""),
            "symbol": getattr(record, "symbol", ""),
            "sector": getattr(record, "sector", ""),
            "price": getattr(record, "price", None),
            "change_value": getattr(record, "change_value", None),
            "change_percent": getattr(record, "change_percent", None),
            "volume": getattr(record, "volume", None),
        }

    row = SimpleNamespace(**payload)
    row.status = status
    return row


def _unique_records(pdf_document):
    records_by_symbol = {}
    if pdf_document:
        for record in pdf_document.extracted_companies.order_by("-volume", "pk"):
            if record.symbol:
                records_by_symbol.setdefault(record.symbol, record)
    return list(records_by_symbol.values())


def _percentage(count, total):
    return round((count / total) * 100) if total else 0


@module_permission_required("dashboard")
def home(request):
    requested_raw = request.GET.get("date")
    requested_date = _parse_report_date(requested_raw)
    invalid_date = bool(requested_raw and requested_date is None)
    selected_pdf = None if invalid_date else _get_latest_pdf_for_date(requested_date)
    if not requested_raw:
        selected_pdf = _get_latest_pdf_for_date()

    records = _unique_records(selected_pdf)
    comparison_rows = _build_comparison_results(selected_pdf) if selected_pdf else []
    status_counts = {
        status: sum(1 for row in comparison_rows if row["status"] == status)
        for status in ("NEW", "REMOVED", "EXISTING")
    }
    gainers = sorted(
        (record for record in records if (record.change_percent or 0) > 0),
        key=lambda record: record.change_percent,
        reverse=True,
    )[:3]
    losers = sorted(
        (record for record in records if (record.change_percent or 0) < 0),
        key=lambda record: record.change_percent,
    )[:3]
    volume_leaders = sorted(records, key=lambda record: record.volume or 0, reverse=True)[:3]
    advancers = len([record for record in records if (record.change_percent or 0) > 0])
    decliners = len([record for record in records if (record.change_percent or 0) < 0])
    unchanged = len(records) - advancers - decliners

    sectors = {}
    for record in records:
        if record.sector and record.change_percent is not None:
            sectors.setdefault(record.sector, []).append(record.change_percent)
    sector_performance = [
        {"name": sector, "average": sum(values) / len(values)}
        for sector, values in sectors.items()
    ]
    sector_performance.sort(key=lambda item: abs(item["average"]), reverse=True)
    max_sector_change = max((abs(item["average"]) for item in sector_performance), default=Decimal("1"))
    for item in sector_performance:
        item["height"] = max(10, round((abs(item["average"]) / max_sector_change) * 100))

    breadth = [
        {"label": "Advancers", "count": advancers, "height": _percentage(advancers, len(records))},
        {"label": "Decliners", "count": decliners, "height": _percentage(decliners, len(records))},
        {"label": "Unchanged", "count": unchanged, "height": _percentage(unchanged, len(records))},
    ]
    leader = max(sector_performance, key=lambda item: item["average"], default=None)
    insights = []
    if records:
        breadth_label = "positive" if advancers > decliners else "negative" if decliners > advancers else "balanced"
        insights.append(
            f"Market breadth is {breadth_label}: {_percentage(advancers, len(records))}% advancers and "
            f"{_percentage(decliners, len(records))}% decliners."
        )
        if leader:
            insights.append(f"{leader['name']} leads sector performance at {leader['average']:+.2f}% average change.")
        if volume_leaders:
            insights.append(f"{volume_leaders[0].symbol} has the highest volume at {volume_leaders[0].volume or 0:,} shares.")

    company = get_client_company(request.user)
    alerts = company.alert_history.select_related("company")[:5] if company else AlertHistory.objects.select_related("company")[:5]
    return render(request, "index.html", {
        "selected_pdf": selected_pdf,
        "invalid_date": invalid_date,
        "available_dates": list(PDFDocument.objects.filter(is_processed=True, report_date__isnull=False).values_list("report_date", flat=True).order_by("-report_date").distinct()),
        "total_stocks": len(records),
        "new_count": status_counts["NEW"],
        "removed_count": status_counts["REMOVED"],
        "existing_count": status_counts["EXISTING"],
        "total_volume": sum((record.volume or 0) for record in records),
        "gainers": gainers,
        "losers": losers,
        "volume_leaders": volume_leaders,
        "breadth": breadth,
        "sector_performance": sector_performance[:6],
        "insights": insights,
        "alerts": alerts,
    })


@module_permission_required("company_analysis")
def company_analysis(request):
    """Show a company's latest saved record and its saved PDF history.

    The PDF processing flow already stores every extracted company row in
    ``ExtractedCompanyRecord``.  Company Analysis reads those stored rows, so
    no duplicate company data needs to be saved for this screen.
    """
    search_query = (request.GET.get("q") or "").strip()
    requested_date = request.GET.get("date") or request.GET.get("report_date") or request.GET.get("selected_date")
    selected_date = _parse_report_date(requested_date)
    invalid_date = bool(requested_date and selected_date is None)

    available_dates = list(
        PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
        .values_list("report_date", flat=True)
        .order_by("-report_date")
        .distinct()
    )

    if invalid_date:
        selected_pdf = None
    elif selected_date is not None:
        selected_pdf = _get_latest_pdf_for_date(selected_date)
    else:
        selected_pdf = _get_latest_pdf_for_date()
        selected_date = selected_pdf.report_date if selected_pdf is not None else None

    all_records = ExtractedCompanyRecord.objects.select_related("pdf_document").filter(
        pdf_document__is_processed=True
    )
    records = all_records.none() if selected_pdf is None else all_records.filter(pdf_document=selected_pdf)

    selected_record = None
    if search_query:
        selected_record = records.filter(symbol__iexact=search_query).order_by(
            "-pdf_document__report_date", "-pdf_document__uploaded_at"
        ).first()
        if selected_record is None:
            selected_record = records.filter(symbol__icontains=search_query).order_by(
                "-pdf_document__report_date", "-pdf_document__uploaded_at"
            ).first()
        if selected_record is None:
            selected_record = records.filter(company_name__icontains=search_query).order_by(
                "-pdf_document__report_date", "-pdf_document__uploaded_at"
            ).first()
    else:
        selected_record = records.order_by(
            "-pdf_document__report_date", "-pdf_document__uploaded_at", "-volume"
        ).first()

    history = []
    all_history = []
    previous_record = None
    chart_points = []
    history_page_obj = None
    history_paginator = None
    if selected_record is not None:
        raw_history = list(
            all_records.filter(symbol__iexact=selected_record.symbol).order_by(
                "-pdf_document__report_date", "-pdf_document__uploaded_at", "-pk"
            )
        )
        seen_dates = set()
        all_history = []
        for record in raw_history:
            report_date = record.pdf_document.report_date
            if report_date in seen_dates:
                continue
            seen_dates.add(report_date)
            all_history.append(record)
        previous_record = next(
            (
                record for record in all_history
                if record.pdf_document.report_date < selected_record.pdf_document.report_date
            ),
            None,
        )

        history_paginator = Paginator(all_history, 5)
        page_number = request.GET.get("page", 1)
        history_page_obj = history_paginator.get_page(page_number)
        history = list(history_page_obj.object_list)

        chart_records = list(reversed(all_history[:6]))
        prices = [record.price for record in chart_records if record.price is not None]
        highest_price = max(prices) if prices else None
        for record in chart_records:
            height = 10
            if highest_price and record.price is not None:
                height = max(10, int((record.price / highest_price) * 100))
            chart_points.append({
                "label": record.pdf_document.report_date.strftime("%d-%b"),
                "height": height,
                "price": record.price,
            })

    if request.GET.get("download") == "csv" and selected_record is not None:
        if not has_module_permission(request.user, "company_analysis", "export"):
            raise PermissionDenied("You do not have permission to export company analysis data.")
        response = HttpResponse(content_type="text/csv")
        safe_symbol = "".join(char for char in selected_record.symbol if char.isalnum() or char in ("_", "-"))
        response["Content-Disposition"] = f'attachment; filename="{safe_symbol or "company"}-analysis.csv"'
        writer = csv.writer(response)
        writer.writerow(["Symbol", "Company", "Report Date", "Price", "Change Value", "Change Percent", "Volume"])
        for record in all_history:
            writer.writerow([
                record.symbol,
                record.company_name,
                record.pdf_document.report_date,
                record.price,
                record.change_value,
                record.change_percent,
                record.volume,
            ])
        return response

    context = {
        "search_query": search_query,
        "company": selected_record,
        "previous_record": previous_record,
        "history": history,
        "history_count": len(all_history),
        "history_page_obj": history_page_obj,
        "history_paginator": history_paginator,
        "chart_points": chart_points,
        "result_not_found": bool(search_query and selected_record is None),
        "invalid_date": invalid_date,
        "selected_date": selected_date,
        "available_dates": available_dates,
        "is_watchlisted": bool(
            selected_record
            and request.user.is_authenticated
            and get_client_company(request.user)
            and WatchlistEntry.objects.filter(
                user=request.user,
                company=get_client_company(request.user),
                symbol=selected_record.symbol,
                is_active=True,
            ).exists()
        ),
    }
    return render(request, "pages/company-analysis.html", context)


def _parse_report_date(raw_value):
    if not raw_value:
        return None

    if hasattr(raw_value, "date"):
        return raw_value.date()

    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return None

        for date_format in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, date_format).date()  # noqa: DTZ007
            except ValueError:
                continue

    return None


def _get_latest_pdf_for_date(report_date=None):
    queryset = PDFDocument.objects.filter(
        is_processed=True, report_date__isnull=False
    ).prefetch_related("extracted_companies")
    if report_date is not None:
        queryset = queryset.filter(report_date=report_date)

    return queryset.order_by("-report_date", "-uploaded_at").first()


def _get_previous_pdf(current_pdf):
    return (
        PDFDocument.objects.filter(
            is_processed=True, report_date__lt=current_pdf.report_date
        )
        .prefetch_related("extracted_companies")
        .order_by("-report_date", "-uploaded_at")
        .first()
    )


def _build_comparison_results(current_pdf):
    stored_results = (
        ComparisonResult.objects.filter(current_pdf=current_pdf)
        .select_related("previous_pdf", "current_pdf")
        .only(
            "id",
            "previous_pdf_id",
            "current_pdf_id",
            "symbol",
            "company_name",
            "status",
            "previous_price",
            "current_price",
        )
        .order_by("status", "company_name")
    )

    if stored_results.exists():
        return list(stored_results.values("symbol", "company_name", "status", "previous_price", "current_price"))

    previous_pdf = _get_previous_pdf(current_pdf)
    if previous_pdf is None:
        return []

    previous_records = {
        item.symbol: item
        for item in previous_pdf.extracted_companies.only(
            "id",
            "pdf_document_id",
            "symbol",
            "company_name",
            "price",
        )
    }
    current_records = {
        item.symbol: item
        for item in current_pdf.extracted_companies.only(
            "id",
            "pdf_document_id",
            "symbol",
            "company_name",
            "price",
        )
    }

    comparison_rows = []
    for symbol in sorted(previous_records.keys() & current_records.keys()):
        old_record = previous_records[symbol]
        new_record = current_records[symbol]
        comparison_rows.append(
            {
                "symbol": symbol,
                "company_name": new_record.company_name,
                "status": "EXISTING",
                "previous_price": old_record.price,
                "current_price": new_record.price,
            }
        )

    for symbol in sorted(current_records.keys() - previous_records.keys()):
        new_record = current_records[symbol]
        comparison_rows.append(
            {
                "symbol": symbol,
                "company_name": new_record.company_name,
                "status": "NEW",
                "previous_price": None,
                "current_price": new_record.price,
            }
        )

    for symbol in sorted(previous_records.keys() - current_records.keys()):
        old_record = previous_records[symbol]
        comparison_rows.append(
            {
                "symbol": symbol,
                "company_name": old_record.company_name,
                "status": "REMOVED",
                "previous_price": old_record.price,
                "current_price": None,
            }
        )

    return comparison_rows


@module_permission_required("daily_market_explorer")
def daily_market_explorer(request):
    latest_pdf = _get_latest_pdf_for_date()

    requested_date = request.GET.get("date") or request.GET.get("report_date") or request.GET.get("selected_date")
    selected_date = _parse_report_date(requested_date)
    invalid_date = bool(requested_date and selected_date is None)
    if invalid_date:
        selected_pdf = None
    elif requested_date:
        selected_pdf = _get_latest_pdf_for_date(selected_date)
    else:
        selected_pdf = latest_pdf

    if selected_pdf is not None:
        selected_date = selected_pdf.report_date
    else:
        selected_date = None

    available_dates = list(
        PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
        .values_list("report_date", flat=True)
        .order_by("-report_date")
        .distinct()
    )

    selected_status = (request.GET.get("status") or request.GET.get("filter") or "all").lower()
    if selected_status not in {"all", "top50", "new", "removed", "existing", "history"}:
        selected_status = "all"
    page_number = request.GET.get("page", 1)
    history_rows = []

    if selected_status == "history":
        history_documents = (
            PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
            .annotate(company_count=Count("extracted_companies"), total_volume=Sum("extracted_companies__volume"))
            .order_by("-report_date", "-uploaded_at")
        )
        seen_history_dates = set()
        for document in history_documents:
            if document.report_date in seen_history_dates:
                continue
            seen_history_dates.add(document.report_date)
            history_rows.append(document)
        history_paginator = Paginator(history_rows, 10)
        history_page = history_paginator.get_page(page_number)
        history_rows = list(history_page.object_list)
        paginator = history_paginator
        page_obj = history_page
        is_paginated = paginator.num_pages > 1

    if selected_status == "history":
        companies = []
        comparison_results = []
        total_companies = 0
        top50_count = 0
        new_count = 0
        removed_count = 0
        existing_count = 0
        filter_querystring = "&status=history"
    elif selected_pdf is None:
        companies = []
        comparison_results = []
        total_companies = 0
        top50_count = 0
        new_count = 0
        removed_count = 0
        existing_count = 0
        page_obj = None
        paginator = None
        is_paginated = False
        filter_querystring = ""
    else:
        comparison_results = _build_comparison_results(selected_pdf)
        comparison_lookup = {row["symbol"]: row for row in comparison_results}

        current_records = list(
            selected_pdf.extracted_companies.only(
                "id",
                "pdf_document_id",
                "company_name",
                "symbol",
                "sector",
                "price",
                "change_value",
                "change_percent",
                "volume",
            ).order_by("-volume", "company_name")
        )
        current_lookup = {record.symbol: record for record in current_records}

        previous_pdf = _get_previous_pdf(selected_pdf)
        previous_records = []
        previous_lookup = {}
        if previous_pdf is not None:
            previous_records = list(
                previous_pdf.extracted_companies.only(
                    "id",
                    "pdf_document_id",
                    "company_name",
                    "symbol",
                    "sector",
                    "price",
                    "change_value",
                    "change_percent",
                    "volume",
                ).order_by("-volume", "company_name")
            )
            previous_lookup = {record.symbol: record for record in previous_records}

        if selected_status == "top50":
            filtered_records = current_records[:50]
        elif selected_status == "new":
            filtered_records = [
                {
                    "company_name": current_lookup[symbol].company_name,
                    "sector": current_lookup[symbol].sector,
                    "symbol": symbol,
                    "price": current_lookup[symbol].price,
                    "change_value": current_lookup[symbol].change_value,
                    "change_percent": current_lookup[symbol].change_percent,
                    "volume": current_lookup[symbol].volume,
                    "status": "NEW",
                }
                for symbol in sorted(current_lookup.keys())
                if comparison_lookup.get(symbol, {}).get("status") == "NEW"
            ]
        elif selected_status == "removed":
            filtered_records = [
                {
                    "company_name": previous_lookup[symbol].company_name,
                    "sector": previous_lookup[symbol].sector,
                    "symbol": symbol,
                    "price": previous_lookup[symbol].price,
                    "change_value": previous_lookup[symbol].change_value,
                    "change_percent": previous_lookup[symbol].change_percent,
                    "volume": previous_lookup[symbol].volume,
                    "status": "REMOVED",
                }
                for symbol in sorted(previous_lookup.keys())
                if comparison_lookup.get(symbol, {}).get("status") == "REMOVED"
            ]
        elif selected_status == "existing":
            filtered_records = [
                {
                    "company_name": current_lookup[symbol].company_name,
                    "sector": current_lookup[symbol].sector,
                    "symbol": symbol,
                    "price": current_lookup[symbol].price,
                    "change_value": current_lookup[symbol].change_value,
                    "change_percent": current_lookup[symbol].change_percent,
                    "volume": current_lookup[symbol].volume,
                    "status": "EXISTING",
                }
                for symbol in sorted(current_lookup.keys())
                if comparison_lookup.get(symbol, {}).get("status") == "EXISTING"
            ]
        else:
            filtered_records = [
                {
                    "company_name": record.company_name,
                    "sector": record.sector,
                    "symbol": record.symbol,
                    "price": record.price,
                    "change_value": record.change_value,
                    "change_percent": record.change_percent,
                    "volume": record.volume,
                    "status": comparison_lookup.get(record.symbol, {}).get("status", ""),
                }
                for record in current_records
            ]

        paginator = Paginator(filtered_records, 10)
        page_obj = paginator.get_page(page_number)
        companies = CompanyCollection([
            _as_company_row(record, status=getattr(record, "status", ""))
            for record in page_obj.object_list
        ])

        total_companies = len(current_records)
        top50_count = min(total_companies, 50)
        new_count = sum(1 for row in comparison_results if row["status"] == "NEW")
        removed_count = sum(1 for row in comparison_results if row["status"] == "REMOVED")
        existing_count = sum(1 for row in comparison_results if row["status"] == "EXISTING")
        is_paginated = paginator.num_pages > 1

        query_params = []
        if selected_date is not None:
            query_params.append(f"date={selected_date.strftime('%Y-%m-%d')}")
        if selected_status and selected_status != "all":
            query_params.append(f"status={selected_status}")
        filter_querystring = "&" + "&".join(query_params) if query_params else ""

    context = {
        "latest_pdf": latest_pdf,
        "selected_pdf": selected_pdf,
        "selected_date": selected_date,
        "available_dates": available_dates,
        "selected_status": selected_status,
        "invalid_date": invalid_date,
        "history_rows": history_rows,
        "companies": companies,
        "page_obj": page_obj,
        "is_paginated": is_paginated,
        "paginator": paginator,
        "filter_querystring": filter_querystring,
        "comparison_results": comparison_results,
        "total_companies": total_companies,
        "top50_count": top50_count,
        "new_count": new_count,
        "removed_count": removed_count,
        "existing_count": existing_count,
    }
    return render(request, "pages/daily-market-explorer.html", context)


@module_permission_required("market_analytics")
def market_analysis(request):
    """Apply every visible analytics filter together to real saved market rows."""
    raw_date = (request.GET.get("date") or "").strip()
    raw_date_from = (request.GET.get("date_from") or "").strip()
    raw_date_to = (request.GET.get("date_to") or "").strip()
    requested_date = _parse_report_date(raw_date)
    date_from = _parse_report_date(raw_date_from)
    date_to = _parse_report_date(raw_date_to)
    range_requested = bool(raw_date_from or raw_date_to)
    invalid_date = bool(
        (raw_date and requested_date is None)
        or (raw_date_from and date_from is None)
        or (raw_date_to and date_to is None)
        or (date_from and date_to and date_from > date_to)
    )
    range_documents = PDFDocument.objects.none()
    if invalid_date:
        selected_pdf = None
    elif range_requested:
        range_documents = PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
        if date_from:
            range_documents = range_documents.filter(report_date__gte=date_from)
        if date_to:
            range_documents = range_documents.filter(report_date__lte=date_to)
        selected_pdf = range_documents.order_by("-report_date", "-uploaded_at").first()
    else:
        selected_pdf = _get_latest_pdf_for_date(requested_date)
    if not raw_date and not range_requested:
        selected_pdf = _get_latest_pdf_for_date()

    filters = {
        "q": (request.GET.get("q") or "").strip(),
        "symbol": (request.GET.get("symbol") or "").strip(),
        "sector": (request.GET.get("sector") or "").strip(),
        "status": (request.GET.get("status") or "").strip().upper(),
        "trend": (request.GET.get("trend") or "").strip().lower(),
        "min_price": (request.GET.get("min_price") or "").strip(),
        "max_price": (request.GET.get("max_price") or "").strip(),
        "min_change": (request.GET.get("min_change") or "").strip(),
        "max_change": (request.GET.get("max_change") or "").strip(),
        "min_volume": (request.GET.get("min_volume") or "").strip(),
        "max_volume": (request.GET.get("max_volume") or "").strip(),
        "date_from": raw_date_from,
        "date_to": raw_date_to,
    }
    queryset = ExtractedCompanyRecord.objects.none()
    filter_errors = []
    if date_from and date_to and date_from > date_to:
        filter_errors.append("Start date must be on or before end date.")
    elif raw_date_from and date_from is None:
        filter_errors.append("Start date is invalid.")
    elif raw_date_to and date_to is None:
        filter_errors.append("End date is invalid.")
    if selected_pdf is None:
        for input_name in ("min_price", "max_price", "min_change", "max_change", "min_volume", "max_volume"):
            if not filters[input_name]:
                continue
            try:
                value = Decimal(filters[input_name])
                if "volume" in input_name and value < 0:
                    raise ValueError
            except (ValueError, ArithmeticError):
                filter_errors.append(f"{input_name.replace('_', ' ').title()} must be a valid non-negative number.")
        if filters["trend"] and filters["trend"] not in {"up", "down", "neutral"}:
            filter_errors.append("Trend must be Up, Down, or Neutral.")
    if selected_pdf:
        queryset = (
            ExtractedCompanyRecord.objects.filter(pdf_document__in=range_documents)
            if range_requested
            else selected_pdf.extracted_companies.all()
        )
        if filters["q"]:
            queryset = queryset.filter(Q(symbol__icontains=filters["q"]) | Q(company_name__icontains=filters["q"]))
        if filters["symbol"]:
            symbols = sorted({value.strip().upper() for value in filters["symbol"].split(",") if value.strip()})
            queryset = queryset.filter(symbol__in=symbols)
        if filters["sector"]:
            sectors = sorted({value.strip() for value in filters["sector"].split(",") if value.strip()})
            sector_query = Q()
            for sector in sectors:
                sector_query |= Q(sector__iexact=sector)
            queryset = queryset.filter(sector_query)

        numeric_filters = (
            ("min_price", "price__gte"), ("max_price", "price__lte"),
            ("min_change", "change_percent__gte"), ("max_change", "change_percent__lte"),
            ("min_volume", "volume__gte"), ("max_volume", "volume__lte"),
        )
        for input_name, lookup in numeric_filters:
            if not filters[input_name]:
                continue
            try:
                value = Decimal(filters[input_name])
                if "volume" in input_name and value < 0:
                    raise ValueError
                queryset = queryset.filter(**{lookup: value})
            except (ValueError, ArithmeticError):
                filter_errors.append(f"{input_name.replace('_', ' ').title()} must be a valid non-negative number.")

        if filters["trend"] == "up":
            queryset = queryset.filter(change_percent__gt=0)
        elif filters["trend"] == "down":
            queryset = queryset.filter(change_percent__lt=0)
        elif filters["trend"] == "neutral":
            queryset = queryset.filter(change_percent=0)
        elif filters["trend"]:
            filter_errors.append("Trend must be Up, Down, or Neutral.")

    comparison_rows = _build_comparison_results(selected_pdf) if selected_pdf else []
    status_lookup = {row["symbol"]: row["status"] for row in comparison_rows}
    records = []
    seen_symbols = set()
    record_ordering = (
        "symbol", "-pdf_document__report_date", "-pdf_document__uploaded_at", "-volume"
    ) if range_requested else ("-volume", "symbol")
    for record in queryset.select_related("pdf_document").order_by(*record_ordering):
        if not record.symbol or record.symbol in seen_symbols:
            continue
        seen_symbols.add(record.symbol)
        record.market_status = status_lookup.get(record.symbol, "")
        if filters["status"] and record.market_status != filters["status"]:
            continue
        records.append(record)

    if selected_pdf and filters["status"] == "REMOVED":
        removed_symbols = {row["symbol"] for row in comparison_rows if row["status"] == "REMOVED"}
        previous_pdf = _get_previous_pdf(selected_pdf)
        removed_records = []
        seen_removed_symbols = set()
        symbol_filter = {value.strip().upper() for value in filters["symbol"].split(",") if value.strip()}
        sector_filter = {value.strip().lower() for value in filters["sector"].split(",") if value.strip()}

        def matches_removed(record):
            if record.symbol not in removed_symbols:
                return False
            if filters["q"] and filters["q"].lower() not in f"{record.symbol} {record.company_name}".lower():
                return False
            if symbol_filter and record.symbol.upper() not in symbol_filter:
                return False
            if sector_filter and (record.sector or "").lower() not in sector_filter:
                return False
            numeric_checks = (
                ("min_price", record.price, lambda actual, expected: actual >= expected),
                ("max_price", record.price, lambda actual, expected: actual <= expected),
                ("min_change", record.change_percent, lambda actual, expected: actual >= expected),
                ("max_change", record.change_percent, lambda actual, expected: actual <= expected),
                ("min_volume", record.volume, lambda actual, expected: actual >= expected),
                ("max_volume", record.volume, lambda actual, expected: actual <= expected),
            )
            for input_name, actual, comparison in numeric_checks:
                if filters[input_name]:
                    try:
                        if actual is None or not comparison(Decimal(actual), Decimal(filters[input_name])):
                            return False
                    except (ValueError, ArithmeticError):
                        return False
            if filters["trend"] == "up" and not ((record.change_percent or 0) > 0):
                return False
            if filters["trend"] == "down" and not ((record.change_percent or 0) < 0):
                return False
            if filters["trend"] == "neutral" and record.change_percent != 0:
                return False
            return True

        if previous_pdf:
            for record in previous_pdf.extracted_companies.order_by("-volume", "symbol"):
                if matches_removed(record) and record.symbol not in seen_removed_symbols:
                    record.market_status = "REMOVED"
                    removed_records.append(record)
                    seen_removed_symbols.add(record.symbol)
        records = removed_records

    if filters["status"] and filters["status"] not in {"NEW", "REMOVED", "EXISTING"}:
        filter_errors.append("Status must be New, Removed, or Existing.")
        records = []

    total_stocks = len(records)
    price_up = [record for record in records if record.change_percent is not None and record.change_percent > 0]
    price_down = [record for record in records if record.change_percent is not None and record.change_percent < 0]
    unchanged = [record for record in records if record.change_percent == 0]
    top_momentum = sorted(records, key=lambda record: record.volume or 0, reverse=True)[:10]
    biggest_gainer = max(price_up, key=lambda record: record.change_percent, default=None)
    biggest_loser = min(price_down, key=lambda record: record.change_percent, default=None)

    sector_groups = {}
    for record in records:
        if record.sector and record.change_percent is not None:
            sector_groups.setdefault(record.sector, []).append(record)
    sector_data = []
    max_change = max(
        (abs(sum((record.change_percent for record in sector_records), Decimal("0")) / len(sector_records)) for sector_records in sector_groups.values()),
        default=Decimal("1"),
    ) or Decimal("1")
    for sector, sector_records in sector_groups.items():
        average = sum((record.change_percent for record in sector_records), Decimal("0")) / len(sector_records)
        sector_data.append({
            "name": sector,
            "change_percent": average,
            "avg_change": average,
            "count": len(sector_records),
            "intensity": max(12, min(100, round((abs(average) / max_change) * 100))),
            "direction": "positive" if average > 0 else "negative" if average < 0 else "neutral",
            "height": max(12, min(100, round((abs(average) / max_change) * 100))),
            "color": "16,185,129" if average > 0 else "239,68,68" if average < 0 else "148,163,184",
            "opacity": str(max(Decimal("0.12"), min(Decimal("0.72"), abs(average) / max_change * Decimal("0.72")))),
        })
    sector_data.sort(key=lambda item: item["count"], reverse=True)

    paginator = Paginator(records, 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    active_filters = [{"label": key.replace("_", " ").title(), "value": value} for key, value in filters.items() if value]
    if raw_date:
        active_filters.insert(0, {"label": "Date", "value": raw_date})

    return render(request, "pages/market-analytics.html", {
        "selected_pdf": selected_pdf,
        "previous_pdf": _get_previous_pdf(selected_pdf) if selected_pdf else None,
        "available_dates": list(PDFDocument.objects.filter(is_processed=True, report_date__isnull=False).values_list("report_date", flat=True).order_by("-report_date").distinct()),
        "available_sectors": list(ExtractedCompanyRecord.objects.exclude(sector="").values_list("sector", flat=True).distinct().order_by("sector")),
        "filters": filters,
        "filter_errors": filter_errors,
        "invalid_date": invalid_date,
        "active_filters": active_filters,
        "analytics_records": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "filter_querystring": query_params.urlencode(),
        "total_stocks": total_stocks,
        "price_up_count": len(price_up),
        "price_down_count": len(price_down),
        "unchanged_count": len(unchanged),
        "new_entry_count": sum(1 for record in records if record.market_status == "NEW"),
        "price_up_percentage": _percentage(len(price_up), total_stocks),
        "price_down_percentage": _percentage(len(price_down), total_stocks),
        "unchanged_percentage": _percentage(len(unchanged), total_stocks),
        "breadth_bars": [
            {"label": "Advancers", "height": _percentage(len(price_up), total_stocks)},
            {"label": "Decliners", "height": _percentage(len(price_down), total_stocks)},
            {"label": "Unchanged", "height": _percentage(len(unchanged), total_stocks)},
        ],
        "sector_data": sector_data,
        "sector_counts": sector_data,
        "sector_company_data": [
            {
                "name": item["name"],
                "count": item["count"],
                "top_companies": [
                    f"{record.company_name} ({record.change_percent:+.2f}%)"
                    for record in sorted(
                        sector_groups[item["name"]],
                        key=lambda record: record.change_percent,
                        reverse=True,
                    )[:3]
                ],
            }
            for item in sector_data[:6]
        ],
        "top_momentum": top_momentum,
        "biggest_gainer": biggest_gainer,
        "biggest_loser": biggest_loser,
    })


@module_permission_required("market_comparison")
def market_comparison(request):
    raw_date = request.GET.get("date")
    requested_date = _parse_report_date(raw_date)
    invalid_date = bool(raw_date and requested_date is None)
    current_pdf = _get_latest_pdf_for_date(requested_date) if requested_date else None
    if current_pdf is None and not raw_date:
        current_pdf = _get_latest_pdf_for_date()

    previous_pdf = _get_previous_pdf(current_pdf) if current_pdf else None
    comparison_rows = _build_comparison_results(current_pdf) if current_pdf else []
    total_stocks = current_pdf.extracted_companies.count() if current_pdf else 0

    current_sector_lookup = {}
    previous_sector_lookup = {}
    if current_pdf:
        current_sector_lookup = {
            record.symbol: (record.sector or "").strip()
            for record in current_pdf.extracted_companies.all()
            if getattr(record, "symbol", None)
        }
    if previous_pdf:
        previous_sector_lookup = {
            record.symbol: (record.sector or "").strip()
            for record in previous_pdf.extracted_companies.all()
            if getattr(record, "symbol", None)
        }

    price_changes, new_entries, removed_entries = [], [], []
    for row in comparison_rows:
        symbol = row.get("symbol")
        if row["status"] == "NEW":
            row["sector"] = current_sector_lookup.get(symbol, "")
            new_entries.append(row)
            continue
        if row["status"] == "REMOVED":
            row["sector"] = previous_sector_lookup.get(symbol, "")
            removed_entries.append(row)
            continue

        row["sector"] = current_sector_lookup.get(symbol, previous_sector_lookup.get(symbol, ""))
        previous_price, current_price = row["previous_price"], row["current_price"]
        if previous_price is None or current_price is None or previous_price == 0:
            continue
        price_changes.append({
            **row,
            "change_percent": ((current_price - previous_price) / previous_price) * Decimal("100"),
        })

    price_up = [row for row in price_changes if row["change_percent"] > 0]
    price_down = [row for row in price_changes if row["change_percent"] < 0]
    unchanged = [row for row in price_changes if row["change_percent"] == 0]
    comparable_total = len(price_changes)

    def percentage(count):
        return round((count / comparable_total) * 100) if comparable_total else 0

    sector_rows = []
    current_sector_counts = {}
    previous_sector_counts = {}

    if current_pdf:
        for record in current_pdf.extracted_companies.all():
            sector_name = (record.sector or "").strip()
            if not sector_name:
                continue
            current_sector_counts[sector_name] = current_sector_counts.get(sector_name, 0) + 1

    if previous_pdf:
        for record in previous_pdf.extracted_companies.all():
            sector_name = (record.sector or "").strip()
            if not sector_name:
                continue
            previous_sector_counts[sector_name] = previous_sector_counts.get(sector_name, 0) + 1

    all_sectors = sorted(set(current_sector_counts) | set(previous_sector_counts))
    max_sector_count = max(
        max(current_sector_counts.values(), default=0),
        max(previous_sector_counts.values(), default=0),
        1,
    )

    sector_rows = []
    for sector in all_sectors:
        current_count = current_sector_counts.get(sector, 0)
        previous_count = previous_sector_counts.get(sector, 0)
        sector_rows.append({
            "label": sector,
            "current_count": current_count,
            "previous_count": previous_count,
            "height": max(10, round((current_count / max_sector_count) * 100)),
            "previous_height": max(10, round((previous_count / max_sector_count) * 100)),
            "delta": current_count - previous_count,
        })

    sector_rows = sorted(sector_rows, key=lambda item: (item["current_count"] + item["previous_count"]), reverse=True)[:6]

    context = {
        "current_pdf": current_pdf,
        "invalid_date": invalid_date,
        "previous_pdf": previous_pdf,
        "available_dates": list(
            PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
            .values_list("report_date", flat=True)
            .order_by("-report_date")
            .distinct()
        ),
        "total_stocks": total_stocks,
        "price_up_count": len(price_up),
        "price_down_count": len(price_down),
        "unchanged_count": len(unchanged),
        "new_entries": new_entries[:3],
        "removed_entries": removed_entries[:3],
        "new_entry_count": len(new_entries),
        "top_price_changes": sorted(price_changes, key=lambda row: row["change_percent"], reverse=True)[:10],
        "biggest_gainers": sorted(price_up, key=lambda row: row["change_percent"], reverse=True)[:3],
        "movement_bars": [
            {"label": "Advancers", "height": percentage(len(price_up))},
            {"label": "Decliners", "height": percentage(len(price_down))},
            {"label": "Unchanged", "height": percentage(len(unchanged))},
        ],
        "sector_rows": sector_rows,
        "price_up_percentage": percentage(len(price_up)),
        "price_down_percentage": percentage(len(price_down)),
        "unchanged_percentage": percentage(len(unchanged)),
    }
    return render(request, "pages/market-comparison.html", context)


@ratelimit(key="user", rate="5/h", method="POST", block=True)
@module_permission_required("pdf_management")
def pdf_management(request):
    form = PDFDocumentForm()

    if request.method == "POST":
        if not has_module_permission(request.user, "pdf_upload", "create"):
            raise PermissionDenied("You do not have permission to upload PDFs.")
        if not has_module_permission(request.user, "pdf_processing", "create"):
            raise PermissionDenied("You do not have permission to process PDFs.")
        form = PDFDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES.get("file")
            if not uploaded_file:
                messages.error(request, "Please select a PDF file before uploading.")
            else:
                logger.info("PDF upload started for file: %s", uploaded_file.name)
                file_hash = get_file_hash(uploaded_file)
                existing_pdf = PDFDocument.objects.filter(file_hash=file_hash).first()
                if existing_pdf:
                    logger.info("Duplicate PDF upload rejected for user_id=%s", request.user.pk)
                    messages.info(request, "This PDF has already been uploaded.")
                    return redirect("pdf-management")

                pdf_document = form.save(commit=False)
                pdf_document.name = os.path.splitext(uploaded_file.name)[0]
                pdf_document.file_hash = file_hash
                try:
                    with transaction.atomic():
                        pdf_document.save()
                except IntegrityError:
                    pdf_document.file.delete(save=False)
                    logger.info("Concurrent duplicate PDF upload rejected for user_id=%s", request.user.pk)
                    messages.info(request, "This PDF has already been uploaded.")
                    return redirect("pdf-management")
                logger.info("PDF uploaded successfully and saved to database: %s", pdf_document.name)

                try:
                    logger.info("Calling scraping function for: %s", pdf_document.name)
                    records = process_pdf_document(pdf_document)
                    logger.info("Scraping completed. Parsed %s records.", len(records))
                    messages.success(request, "PDF uploaded and processed successfully.")
                except Exception as exc:
                    logger.exception("PDF processing failed for %s", pdf_document.name)
                    pdf_document.processing_error = (
                        str(exc)
                        if django_settings.DEBUG
                        else "Processing failed. Review server logs for details."
                    )
                    pdf_document.is_processed = False
                    pdf_document.save(update_fields=["processing_error", "is_processed"])
                    messages.error(request, "PDF upload completed, but processing failed. The error was logged for an administrator.")

                return redirect("pdf-management")

    pdf_documents = PDFDocument.objects.order_by("-uploaded_at")
    paginator = Paginator(pdf_documents, 5)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    return render(
        request,
        "pages/pdf-management.html",
        {
            "form": form,
            "pdf_documents": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
        },
    )


@module_permission_required("pdf_management")
def pdf_details(request, pk):
    pdf_document = get_object_or_404(PDFDocument, pk=pk)
    rows = list(pdf_document.extracted_companies.all())
    paginator = Paginator(rows, 10)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    table_rows = get_table_data(page_obj.object_list)
    return render(
        request,
        "pages/extracted-data.html",
        {
            "pdf_document": pdf_document,
            "table_rows": table_rows,
            "page_obj": page_obj,
            "paginator": paginator,
        },
    )


@require_POST
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@module_permission_required("pdf_management", "delete")
def delete_pdf(request, pk):
    pdf_document = get_object_or_404(PDFDocument, pk=pk)
    pdf_name = pdf_document.name
    if pdf_document.file:
        pdf_document.file.delete(save=False)
    pdf_document.delete()

    messages.success(request, f"{pdf_name} was deleted. You can upload it again.")
    return redirect("pdf-management")


REPORT_PERIODS = {
    "daily": ("Daily Report", 0),
    "weekly": ("Weekly Report", 6),
    "monthly": ("Monthly Report", 29),
    "quarterly": ("Quarterly Report", 89),
}


def _pdf_text(value):
    """Escape plain text for a minimal, dependency-free PDF document."""
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_report_pdf(title, date_from, date_to, documents):
    total_companies = ExtractedCompanyRecord.objects.filter(
        pdf_document__in=documents
    ).values("symbol").distinct().count()
    # Keep the report useful even when no files have been processed yet.
    lines = [
        "PSX Daily Market Intelligence Engine",
        title,
        f"Period: {date_from:%d-%b-%Y} to {date_to:%d-%b-%Y}",
        f"Processed market files: {documents.count()}",
        f"Unique companies: {total_companies}",
        "Generated from the market data currently available in the system.",
    ]
    stream_lines = ["BT", "/F1 16 Tf", "72 760 Td"]
    for index, line in enumerate(lines):
        if index:
            stream_lines.append("0 -28 Td")
        stream_lines.append(f"({_pdf_text(line)}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return bytes(output)


@ratelimit(key="user", rate="20/h", method="POST", block=True)
@module_permission_required("reports")
def reports(request):
    selected_type = (request.GET.get("type") or "daily").lower()
    if selected_type not in REPORT_PERIODS:
        selected_type = "daily"

    if request.method == "POST":
        if not has_module_permission(request.user, "reports", "create"):
            raise PermissionDenied("You do not have permission to generate reports.")
        report_type = (request.POST.get("report_type") or selected_type).lower()
        if report_type not in REPORT_PERIODS:
            messages.error(request, "Please select a valid report type.")
            return redirect("reports")

        report_name, days_back = REPORT_PERIODS[report_type]
        raw_report_date = request.POST.get("date")
        requested_report_date = _parse_report_date(raw_report_date)
        if raw_report_date and requested_report_date is None:
            messages.error(request, "Please select a valid report date.")
            return redirect(f"{request.path}?type={report_type}")
        latest_pdf = _get_latest_pdf_for_date(requested_report_date) if requested_report_date else _get_latest_pdf_for_date()
        date_to = latest_pdf.report_date if latest_pdf and latest_pdf.report_date else timezone.localdate()
        date_from = date_to - timedelta(days=days_back)
        documents = PDFDocument.objects.filter(
            is_processed=True, report_date__range=(date_from, date_to)
        )
        if not documents.exists():
            messages.error(request, "No processed market data is available for the selected report period.")
            return redirect(f"{request.path}?type={report_type}")
        report = GeneratedReport(
            report_type=report_type,
            name=report_name,
            date_from=date_from,
            date_to=date_to,
            created_by=request.user,
        )
        filename = f"{report_type}-market-report-{date_to:%Y%m%d}.pdf"
        report.file.save(filename, ContentFile(_make_report_pdf(report_name, date_from, date_to, documents)), save=False)
        report.save()
        messages.success(request, f"{report_name} generated successfully.")
        return redirect(f"{request.path}?type={report_type}")

    # Build available dates for the topbar date selector from scraped PDF data
    available_dates = list(
        PDFDocument.objects.filter(is_processed=True, report_date__isnull=False)
        .values_list("report_date", flat=True)
        .order_by("-report_date")
        .distinct()
    )

    # Handle selected date from GET parameter
    raw_selected_date = request.GET.get("date")
    selected_date = _parse_report_date(raw_selected_date)
    invalid_date = bool(raw_selected_date and selected_date is None)
    if selected_date is None and available_dates and not raw_selected_date:
        selected_date = available_dates[0]

    selected_iso = selected_date.isoformat() if selected_date else None

    # Filter recent reports by selected date if provided
    recent_reports = GeneratedReport.objects.all()
    if not (request.user.is_staff or request.user.is_superuser):
        recent_reports = recent_reports.filter(created_by=request.user)
    if selected_date is not None:
        recent_reports = recent_reports.filter(date_to=selected_date)
    recent_reports = recent_reports[:10]

    context = {
        "selected_type": selected_type,
        "report_types": REPORT_PERIODS,
        "recent_reports": recent_reports,
        "latest_pdf": _get_latest_pdf_for_date(),
        "available_dates": [
            {"iso": d.isoformat(), "display": d.strftime("%d-%b-%Y")}
            for d in available_dates
        ],
        "selected_iso": selected_iso,
        "invalid_date": invalid_date,
    }
    return render(request, "pages/reports.html", context)


@module_permission_required("reports", "export")
def download_report(request, pk):
    reports = GeneratedReport.objects.all()
    if not (request.user.is_staff or request.user.is_superuser):
        reports = reports.filter(created_by=request.user)
    report = get_object_or_404(reports, pk=pk)
    try:
        return FileResponse(report.file.open("rb"), as_attachment=True, filename=Path(report.file.name).name)
    except (FileNotFoundError, OSError):
        messages.error(request, "The generated report file is no longer available. Please generate it again.")
        return redirect("reports")


@module_permission_required("search_screener")
def search_screener(request):
    """Run the existing screener UI against the latest saved market report."""
    raw_date = request.GET.get("date")
    selected_date = _parse_report_date(raw_date)
    invalid_date = bool(raw_date and selected_date is None)
    latest_pdf = None if invalid_date else _get_latest_pdf_for_date(selected_date)
    if selected_date is None and latest_pdf:
        selected_date = latest_pdf.report_date
    filters = {
        "q": (request.GET.get("q") or "").strip(),
        "sector": (request.GET.get("sector") or "").strip(),
        "min_price": (request.GET.get("min_price") or "").strip(),
        "max_price": (request.GET.get("max_price") or "").strip(),
        "min_change": (request.GET.get("min_change") or "").strip(),
        "max_change": (request.GET.get("max_change") or "").strip(),
        "min_volume": (request.GET.get("min_volume") or "").strip(),
    }

    records = ExtractedCompanyRecord.objects.none()
    invalid_numeric_filter = False
    if latest_pdf:
        records = latest_pdf.extracted_companies.all()
        if filters["q"]:
            records = records.filter(
                Q(symbol__icontains=filters["q"]) | Q(company_name__icontains=filters["q"])
            )
        if filters["sector"]:
            records = records.filter(sector__icontains=filters["sector"])

        numeric_filters = (
            ("min_price", "price__gte"), ("max_price", "price__lte"),
            ("min_change", "change_percent__gte"), ("max_change", "change_percent__lte"),
            ("min_volume", "volume__gte"),
        )
        for field, lookup in numeric_filters:
            if not filters[field]:
                continue
            try:
                value = Decimal(filters[field])
                if field == "min_volume" and value < 0:
                    raise ValueError
                records = records.filter(**{lookup: value})
            except (ValueError, ArithmeticError):
                messages.warning(request, f"{field.replace('_', ' ').title()} must be a valid number.")
                invalid_numeric_filter = True

        records = records.none() if invalid_numeric_filter else records.order_by("symbol", "company_name")

    paginator = Paginator(records, 10)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    query_params = request.GET.copy()
    query_params.pop("page", None)

    return render(
        request,
        "pages/search-screener.html",
        {
            "records": page_obj.object_list,
            "result_count": paginator.count,
            "filters": filters,
            "latest_pdf": latest_pdf,
            "selected_date": selected_date,
            "invalid_date": invalid_date,
            "page_obj": page_obj,
            "paginator": paginator,
            "filter_querystring": query_params.urlencode(),
        },
    )


def _validated_alert_updates(alerts, post_data):
    """Parse every threshold before saving any alert setting."""
    updates = []
    max_threshold = Decimal("9999999999.99")
    for alert in alerts:
        try:
            threshold = Decimal(post_data.get(f"alert_{alert.alert_type}_threshold", "0"))
            if not threshold.is_finite() or threshold < 0 or threshold > max_threshold:
                return None
        except (ValueError, ArithmeticError):
            return None
        updates.append(
            (
                alert,
                post_data.get(f"alert_{alert.alert_type}_enabled") == "on",
                threshold,
            )
        )
    return updates


@ratelimit(key="user", rate="30/h", method="POST", block=True)
@module_permission_required("settings_profile")
def settings(request):
    create_form = ClientCompanyCreateForm()
    client_company = get_client_company(request.user)
    company_settings = None
    alert_settings = []

    if request.user.is_staff or request.user.is_superuser:
        if request.method == "POST" and request.POST.get("action") == "create_company":
            create_form = ClientCompanyCreateForm(request.POST)
            if create_form.is_valid():
                user = create_form.save()
                messages.success(request, f"Client company {user.client_company.company_name} was created.")
                return redirect("client-company-detail", pk=user.client_company.pk)
            messages.error(request, "Please correct the company registration errors below.")
        companies = ClientCompany.objects.select_related("user").all()
    else:
        companies = ClientCompany.objects.none()
        if client_company is None:
            raise PermissionDenied("This account is not linked to a client company.")
        company_settings, _created = CompanySettings.objects.get_or_create(company=client_company)
        for alert_type, _label in AlertSetting.ALERT_TYPES:
            AlertSetting.objects.get_or_create(company=client_company, alert_type=alert_type)
        alert_settings = client_company.alert_settings.all()
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "update_profile":
                profile_form = ClientProfileUpdateForm(request.POST)
                if profile_form.is_valid():
                    with transaction.atomic():
                        client_company.company_name = profile_form.cleaned_data["company_name"]
                        client_company.contact_name = profile_form.cleaned_data["contact_name"]
                        client_company.phone = profile_form.cleaned_data["phone"]
                        client_company.save(update_fields=["company_name", "contact_name", "phone"])
                        request.user.email = profile_form.cleaned_data["email"]
                        request.user.save(update_fields=["email"])
                    messages.success(request, "Profile information updated.")
                    return redirect("settings")
                messages.error(request, "Please enter valid profile information.")
            elif action == "update_preferences":
                form = CompanySettingsForm(request.POST, instance=company_settings)
                if form.is_valid():
                    form.save()
                    messages.success(request, "Preferences updated.")
                    return redirect("settings")
                messages.error(request, "Please correct the preference values.")
            elif action == "update_alerts":
                updates = _validated_alert_updates(alert_settings, request.POST)
                if updates is not None:
                    with transaction.atomic():
                        for alert, enabled, threshold in updates:
                            alert.enabled = enabled
                            alert.threshold = threshold
                            alert.save(update_fields=["enabled", "threshold"])
                    messages.success(request, "Alert settings updated.")
                    return redirect("settings")
                messages.error(request, "Alert thresholds must be finite non-negative numbers.")

    return render(request, "pages/settings.html", {
        "companies": companies,
        "create_form": create_form,
        "client_company": client_company,
        "company_settings": company_settings,
        "alert_settings": alert_settings,
    })


@ratelimit(key="user", rate="60/h", method="POST", block=True)
@admin_required
def client_company_detail(request, pk):
    client_company = get_object_or_404(ClientCompany.objects.select_related("user"), pk=pk)
    company_settings, _created = CompanySettings.objects.get_or_create(company=client_company)
    for module, _label in MODULE_CHOICES:
        ModulePermission.objects.get_or_create(company=client_company, module=module)
    for alert_type, _label in AlertSetting.ALERT_TYPES:
        AlertSetting.objects.get_or_create(company=client_company, alert_type=alert_type)

    update_form = ClientCompanyUpdateForm(initial={
        "company_name": client_company.company_name,
        "contact_name": client_company.contact_name,
        "email": client_company.user.email,
        "phone": client_company.phone,
        "is_active": client_company.user.is_active,
    })
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_company":
            update_form = ClientCompanyUpdateForm(request.POST)
            if update_form.is_valid():
                data = update_form.cleaned_data
                new_password = (request.POST.get("new_password") or "").strip()
                if new_password:
                    try:
                        validate_password(new_password, user=client_company.user)
                    except ValidationError as exc:
                        messages.error(request, " ".join(exc.messages))
                        return redirect("client-company-detail", pk=pk)
                with transaction.atomic():
                    client_company.company_name = data["company_name"]
                    client_company.contact_name = data["contact_name"]
                    client_company.phone = data["phone"]
                    client_company.save(update_fields=["company_name", "contact_name", "phone"])
                    client_company.user.email = data["email"]
                    client_company.user.is_active = data["is_active"]
                    if new_password:
                        client_company.user.set_password(new_password)
                    client_company.user.save()
                messages.success(request, "Company account updated.")
                return redirect("client-company-detail", pk=pk)
        elif action == "update_permissions":
            with transaction.atomic():
                for permission in client_company.module_permissions.all():
                    prefix = f"permission_{permission.module}_"
                    permission.can_view = request.POST.get(prefix + "view") == "on"
                    permission.can_create = request.POST.get(prefix + "create") == "on"
                    permission.can_edit = request.POST.get(prefix + "edit") == "on"
                    permission.can_delete = request.POST.get(prefix + "delete") == "on"
                    permission.can_export = request.POST.get(prefix + "export") == "on"
                    permission.save()
            messages.success(request, "Company permissions updated.")
            return redirect("client-company-detail", pk=pk)
        elif action == "update_alerts":
            alerts = client_company.alert_settings.all()
            updates = _validated_alert_updates(alerts, request.POST)
            if updates is None:
                messages.error(request, "Alert thresholds must be finite non-negative numbers.")
                return redirect("client-company-detail", pk=pk)
            with transaction.atomic():
                company_settings.email_notifications = request.POST.get("email_notifications") == "on"
                company_settings.watchlist_alerts = request.POST.get("watchlist_alerts") == "on"
                company_settings.save(update_fields=["email_notifications", "watchlist_alerts"])
                for alert, enabled, threshold in updates:
                    alert.enabled = enabled
                    alert.threshold = threshold
                    alert.save(update_fields=["enabled", "threshold"])
            messages.success(request, "Company alert settings updated.")
            return redirect("client-company-detail", pk=pk)

    return render(request, "pages/client-company-detail.html", {
        "client_company": client_company,
        "update_form": update_form,
        "permissions": client_company.module_permissions.all(),
        "company_settings": company_settings,
        "alert_settings": client_company.alert_settings.all(),
        "alerts": client_company.alert_history.all()[:20],
        "watchlist_count": client_company.watchlist_entries.count(),
    })


@ratelimit(key="user", rate="60/h", method="POST", block=True)
@module_permission_required("watchlist")
def watchlist(request):
    client_company = get_client_company(request.user)
    if client_company is None:
        return render(request, "pages/watchlist.html", {"admin_without_company": True, "watchlist_rows": []})

    if request.method == "POST":
        action = request.POST.get("action")
        required_action = "delete" if action in {"remove", "delete"} else "create"
        if not has_module_permission(request.user, "watchlist_management", required_action):
            raise PermissionDenied("You do not have permission to manage the watchlist.")
        symbol = (request.POST.get("symbol") or "").strip().upper()
        if action == "add":
            exists = ExtractedCompanyRecord.objects.filter(
                symbol__iexact=symbol, pdf_document__is_processed=True
            ).exists()
            if not symbol or not exists:
                messages.error(request, "Enter a valid symbol found in saved market data.")
            else:
                latest_company = ExtractedCompanyRecord.objects.filter(
                    symbol__iexact=symbol, pdf_document__is_processed=True
                ).order_by("-pdf_document__report_date", "-pdf_document__uploaded_at").first()
                entry, created = WatchlistEntry.objects.get_or_create(
                    user=request.user,
                    company=client_company,
                    symbol=symbol,
                    defaults={"company_name": latest_company.company_name if latest_company else ""},
                )
                if not created and not entry.is_active:
                    entry.is_active = True
                    entry.company_name = latest_company.company_name if latest_company else entry.company_name
                    entry.save(update_fields=["is_active", "company_name", "updated_at"])
                messages.success(request, f"{symbol} added to your watchlist.") if created or entry.is_active else messages.info(request, f"{symbol} is already on your watchlist.")
        elif action == "remove":
            item = WatchlistEntry.objects.filter(user=request.user, company=client_company, symbol=symbol).first()
            if item is None:
                messages.info(request, f"{symbol} was not on your watchlist.")
            else:
                item.is_active = False
                item.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"{symbol} removed from your watchlist.")
        elif action == "save_rules":
            entry = WatchlistEntry.objects.filter(user=request.user, company=client_company, symbol=symbol).first()
            if entry is None:
                messages.error(request, "Watchlist entry not found.")
            else:
                value_enabled = request.POST.get("value_enabled") == "on"
                occurrence_enabled = request.POST.get("occurrence_enabled") == "on"
                email_enabled = request.POST.get("email_enabled") == "on"
                in_app_enabled = request.POST.get("in_app_enabled") == "on"
                if value_enabled:
                    threshold = request.POST.get("value_threshold") or "2"
                    rule, _ = WatchlistAlertRule.objects.get_or_create(
                        watchlist_entry=entry,
                        alert_type=WatchlistAlertRule.VALUE_DIFFERENCE,
                    )
                    rule.threshold = Decimal(threshold)
                    rule.is_active = True
                    rule.email_enabled = email_enabled
                    rule.in_app_enabled = in_app_enabled
                    rule.save(update_fields=["threshold", "is_active", "email_enabled", "in_app_enabled", "updated_at"])
                else:
                    WatchlistAlertRule.objects.filter(
                        watchlist_entry=entry,
                        alert_type=WatchlistAlertRule.VALUE_DIFFERENCE,
                    ).update(is_active=False)
                if occurrence_enabled:
                    gap = request.POST.get("occurrence_gap_days") or "3"
                    rule, _ = WatchlistAlertRule.objects.get_or_create(
                        watchlist_entry=entry,
                        alert_type=WatchlistAlertRule.DATE_OCCURRENCE,
                    )
                    rule.occurrence_gap_days = int(gap)
                    rule.is_active = True
                    rule.email_enabled = email_enabled
                    rule.in_app_enabled = in_app_enabled
                    rule.save(update_fields=["occurrence_gap_days", "is_active", "email_enabled", "in_app_enabled", "updated_at"])
                else:
                    WatchlistAlertRule.objects.filter(
                        watchlist_entry=entry,
                        alert_type=WatchlistAlertRule.DATE_OCCURRENCE,
                    ).update(is_active=False)
                messages.success(request, f"Alert settings saved for {symbol}.")
        return redirect("watchlist")

    entries = list(WatchlistEntry.objects.filter(user=request.user, company=client_company, is_active=True).select_related("company"))
    symbols = [entry.symbol for entry in entries]
    latest_by_symbol = {}
    for record in ExtractedCompanyRecord.objects.filter(
        symbol__in=symbols, pdf_document__is_processed=True
    ).select_related("pdf_document").order_by("symbol", "-pdf_document__report_date", "-pdf_document__uploaded_at"):
        latest_by_symbol.setdefault(record.symbol, record)
    watchlist_rows = [{
        "entry": entry,
        "record": latest_by_symbol.get(entry.symbol),
        "rules": list(entry.alert_rules.filter(is_active=True).order_by("alert_type")),
    } for entry in entries]
    valid_records = [row["record"] for row in watchlist_rows if row["record"]]
    up_count = sum(1 for record in valid_records if (record.change_percent or 0) > 0)
    down_count = sum(1 for record in valid_records if (record.change_percent or 0) < 0)
    unchanged_count = len(valid_records) - up_count - down_count
    return render(request, "pages/watchlist.html", {
        "watchlist_rows": watchlist_rows,
        "total_count": len(entries),
        "up_count": up_count,
        "down_count": down_count,
        "unchanged_count": unchanged_count,
        "alerts": AlertHistory.objects.filter(user=request.user).select_related("watchlist_entry").order_by("-created_at")[:10],
    })


def import_data_from_folder(folder):
    folder_path = Path(folder)
    if not folder_path.exists():
        return 0

    imported_count = 0
    for json_file in sorted(folder_path.glob("*.json")):
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        for item in payload:
            ScrapedRecord.objects.create(
                symbol=item.get("symbol", ""),
                company=item.get("company", ""),
                sector=item.get("sector", ""),
                price=Decimal(str(item.get("price", "0"))) if item.get("price") is not None else None,
                change_percent=item.get("change_percent", ""),
                volume=int(item.get("volume", 0)) if item.get("volume") is not None else None,
                trend=item.get("trend", ""),
                date=None if not item.get("date") else item.get("date"),
            )
            imported_count += 1

    return imported_count
