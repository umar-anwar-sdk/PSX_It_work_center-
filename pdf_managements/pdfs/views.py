import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from scraper.forms import PDFDocumentForm
from scraper.models import ComparisonResult, ExtractedCompanyRecord, PDFDocument
from scraper.utils import get_file_hash, get_table_data, process_pdf_document

from .models import ScrapedRecord

logger = logging.getLogger(__name__)


def home(request):
    return render(request, "index.html")


def company_analysis(request):
    return render(request, "pages/company-analysis.html")


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
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue

    return None


def _get_latest_pdf_for_date(report_date=None):
    queryset = PDFDocument.objects.filter(report_date__isnull=False).prefetch_related("extracted_companies")
    if report_date is not None:
        queryset = queryset.filter(report_date=report_date)

    return queryset.order_by("-report_date", "-uploaded_at").first()


def _get_previous_pdf(current_pdf):
    return (
        PDFDocument.objects.filter(report_date__lt=current_pdf.report_date)
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


def daily_market_explorer(request):
    latest_pdf = _get_latest_pdf_for_date()

    requested_date = request.GET.get("date") or request.GET.get("report_date") or request.GET.get("selected_date")
    selected_date = _parse_report_date(requested_date)
    selected_pdf = _get_latest_pdf_for_date(selected_date) if selected_date else None

    if selected_pdf is None:
        selected_pdf = latest_pdf

    if selected_pdf is not None:
        selected_date = selected_pdf.report_date
    else:
        selected_date = None

    available_dates = list(
        PDFDocument.objects.filter(report_date__isnull=False)
        .values_list("report_date", flat=True)
        .order_by("-report_date")
        .distinct()
    )

    selected_status = (request.GET.get("status") or request.GET.get("filter") or "all").lower()
    page_number = request.GET.get("page", 1)

    if selected_pdf is None:
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
        companies = list(page_obj.object_list)

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


def market_analysis(request):
    return render(request, "pages/market-analytics.html")


def market_comparison(request):
    return render(request, "pages/market-comparison.html")


def pdf_management(request):
    form = PDFDocumentForm()

    if request.method == "POST":
        form = PDFDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES.get("file")
            if not uploaded_file:
                messages.error(request, "Please select a PDF file before uploading.")
            else:
                logger.info("PDF upload started for file: %s", uploaded_file.name)
                file_hash = get_file_hash(uploaded_file)
                logger.info("Generated file hash: %s", file_hash)
                existing_pdf = PDFDocument.objects.filter(file_hash=file_hash).first()
                if existing_pdf:
                    logger.info("Duplicate PDF detected for hash: %s", file_hash)
                    messages.info(request, "This PDF has already been uploaded.")
                    return redirect("pdf-management")

                pdf_document = form.save(commit=False)
                pdf_document.name = os.path.splitext(uploaded_file.name)[0]
                pdf_document.file_hash = file_hash
                pdf_document.save()
                logger.info("PDF uploaded successfully and saved to database: %s", pdf_document.name)

                try:
                    logger.info("Calling scraping function for: %s", pdf_document.name)
                    records = process_pdf_document(pdf_document)
                    logger.info("Scraping completed. Parsed %s records.", len(records))
                    messages.success(request, "PDF uploaded and processed successfully.")
                except Exception as exc:
                    logger.exception("PDF processing failed for %s", pdf_document.name)
                    pdf_document.processing_error = str(exc)
                    pdf_document.is_processed = False
                    pdf_document.save(update_fields=["processing_error", "is_processed"])
                    messages.error(request, f"PDF upload completed, but processing failed: {exc}")

                return redirect("pdf-management")

    pdf_documents = PDFDocument.objects.order_by("-uploaded_at")
    return render(request, "pages/pdf-management.html", {"form": form, "pdf_documents": pdf_documents})


def pdf_details(request, pk):
    pdf_document = get_object_or_404(PDFDocument, pk=pk)
    table_rows = get_table_data(pdf_document.extracted_companies.all())
    return render(request, "pages/extracted-data.html", {"pdf_document": pdf_document, "table_rows": table_rows})


def reports(request):
    return render(request, "pages/reports.html")


def search_screener(request):
    return render(request, "pages/search-screener.html")


def settings(request):
    return render(request, "pages/settings.html")


def watchlist(request):
    return render(request, "pages/watchlist.html")


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
