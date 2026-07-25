import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from scraper.forms import PDFDocumentForm
from scraper.models import PDFDocument, ComparisonResult
from scraper.utils import get_file_hash, get_table_data, process_pdf_document

from .models import ScrapedRecord

logger = logging.getLogger(__name__)


def home(request):
    return render(request, "index.html")


def company_analysis(request):
    return render(request, "pages/company-analysis.html")


def daily_market_explorer(request):
    return render(request, "pages/daily-market-explorer.html")


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
