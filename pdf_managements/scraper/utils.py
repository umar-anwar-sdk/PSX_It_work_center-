import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
from django.utils import timezone

from .models import (
    ExtractedCompanyRecord,
    PDFDocument,
    ComparisonResult,
)

logger = logging.getLogger(__name__)


def clean_text(text):
    if not text:
        return ""

    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pdf_text(pdf_path):
    if hasattr(pdf_path, "path"):
        pdf_path = pdf_path.path

    if not pdf_path:
        logger.error("No PDF path was provided to extract_pdf_text.")
        return ""

    logger.info("Starting PDF text extraction for: %s", pdf_path)
    text_parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                extracted_text = page.extract_text() or ""
                if extracted_text:
                    text_parts.append(extracted_text)
                    logger.info("Extracted page %s from PDF", page_number)
    except Exception as exc:
        logger.exception("PDF text extraction failed for %s", pdf_path)
        raise

    full_text = "\n".join(text_parts)
    logger.info("PDF text extraction completed. Total characters: %s", len(full_text))
    return full_text


def extract_report_datetime(pdf_path):
    text = extract_pdf_text(pdf_path)
    report_date = None
    report_time = None

    date_match = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", text)
    time_match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)", text, re.I)

    if date_match:
        for date_format in ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"]:
            try:
                report_date = datetime.strptime(date_match.group(1), date_format).date()
                break
            except ValueError:
                continue

    if time_match:
        for time_format in ["%H:%M:%S", "%H:%M", "%I:%M %p"]:
            try:
                report_time = datetime.strptime(time_match.group(1), time_format).time()
                break
            except ValueError:
                continue

    return report_date, report_time


def parse_pdf_text(text):
    records = []
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]

    for line in lines:
        if not re.search(r"\d", line):
            continue

        if line.lower().startswith(("psx", "daily", "weekly", "sr#", "symbol", "name", "sector")):
            continue

        if "page" in line.lower() and "http" in line.lower():
            continue

        if re.match(r"^\d+\s+[A-Z]{2,}\s+", line):
            parts = line.split()
            if len(parts) < 7:
                continue

            symbol = parts[1]
            company_name = parts[2]
            sector = " ".join(parts[3:-5])
            price_text = parts[-5]
            change_text = parts[-4]
            change_percent_text = parts[-3]
            volume_text = parts[-2]
            trend = parts[-1]

            company_name = company_name if company_name != "Unknown" else ""
            if not company_name:
                continue

            cleaned_percent = change_percent_text.replace("%", "")
            cleaned_volume = volume_text.replace(",", "")
            cleaned_change = re.sub(r"[^0-9.-]", "", change_text)

            try:
                records.append(
                    {
                        "company_name": company_name,
                        "symbol": symbol,
                        "price": Decimal(price_text),
                        "change_value": Decimal(cleaned_change),
                        "change_percent": Decimal(cleaned_percent),
                        "volume": int(cleaned_volume),
                    }
                )
            except Exception:
                continue

    logger.info("Parsed %s company rows from PDF text", len(records))
    return records


def extract_company_information(pdf_path):
    text = extract_pdf_text(pdf_path)
    return parse_pdf_text(text)


def get_file_hash(file):
    hasher = hashlib.sha256()
    if hasattr(file, "seek"):
        file.seek(0)
    if hasattr(file, "chunks"):
        for chunk in file.chunks():
            hasher.update(chunk)
    else:
        hasher.update(str(file).encode("utf-8"))
    if hasattr(file, "seek"):
        file.seek(0)
    return hasher.hexdigest()


def save_extracted_data(pdf_document, records):
    logger.info("Saving %s extracted records for PDF %s", len(records), pdf_document.name)
    pdf_document.extracted_companies.all().delete()

    for record in records:
        ExtractedCompanyRecord.objects.create(
            pdf_document=pdf_document,
            company_name=record.get("company_name", ""),
            symbol=record.get("symbol", ""),
            price=record.get("price"),
            change_value=record.get("change_value"),
            change_percent=record.get("change_percent"),
            volume=record.get("volume"),
        )

    logger.info("Saved extracted data for PDF %s", pdf_document.name)


def process_pdf_document(pdf_document):
    logger.info("Scraping started for PDF: %s", pdf_document.name)
    file_path = pdf_document.file.path
    logger.info("PDF path: %s", file_path)

    report_date, report_time = extract_report_datetime(file_path)
    logger.info("Extracted report date/time: %s / %s", report_date, report_time)

    records = extract_company_information(file_path)
    logger.info("Parsed %s records", len(records))

    pdf_document.report_date = report_date
    pdf_document.report_time = report_time
    if not pdf_document.file_hash:
        pdf_document.file_hash = get_file_hash(pdf_document.file)
    pdf_document.is_processed = True
    pdf_document.processing_error = ""
    pdf_document.processed_at = timezone.now()
    pdf_document.save()
    logger.info("Saved PDF metadata for: %s", pdf_document.name)
    save_extracted_data(pdf_document, records)
    compare_with_previous_pdf(pdf_document)
    return records


def scrape_pdf(pdf_file_path):
    report_date, report_time = extract_report_datetime(pdf_file_path)
    records = extract_company_information(pdf_file_path)
    return {
        "success": True,
        "report_date": report_date,
        "report_time": report_time,
        "records": records,
        "error": None,
    }


def get_table_data(records):
    rows = []
    for item in records:
        if hasattr(item, "company_name"):
            record = item
            payload = {
                "company_name": record.company_name,
                "symbol": record.symbol,
                "price": record.price,
                "change_value": record.change_value,
                "change_percent": record.change_percent,
                "volume": record.volume,
            }
        else:
            payload = item

        rows.append(
            {
                "company_name": payload.get("company_name", ""),
                "symbol": payload.get("symbol", ""),
                "price": format_decimal(payload.get("price")),
                "change_value": format_decimal(payload.get("change_value")),
                "change_percent": format_decimal(payload.get("change_percent")),
                "volume": payload.get("volume", ""),
            }
        )
    return rows


def format_decimal(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, ".2f")
    return str(value)




def compare_with_previous_pdf(current_pdf):
    """
    Compare current PDF with the most recent previous processed PDF.
    """

    previous_pdf = (
        PDFDocument.objects.filter(
            is_processed=True,
            report_date__lt=current_pdf.report_date,
        )
        .order_by("-report_date", "-report_time")
        .first()
    )

    # First PDF hai to compare nahi hoga
    if previous_pdf is None:
        logger.info("No previous PDF found. Skipping comparison.")
        return

    previous_records = {
        item.symbol: item
        for item in previous_pdf.extracted_companies.all()
    }

    current_records = {
        item.symbol: item
        for item in current_pdf.extracted_companies.all()
    }

    # Agar dobara compare ho to purana result delete kar do
    ComparisonResult.objects.filter(current_pdf=current_pdf).delete()

    # Existing
    for symbol in previous_records.keys() & current_records.keys():

        old = previous_records[symbol]
        new = current_records[symbol]

        ComparisonResult.objects.create(
            previous_pdf=previous_pdf,
            current_pdf=current_pdf,
            symbol=symbol,
            company_name=new.company_name,
            status="EXISTING",
            previous_price=old.price,
            current_price=new.price,
        )

    # New
    for symbol in current_records.keys() - previous_records.keys():

        new = current_records[symbol]

        ComparisonResult.objects.create(
            previous_pdf=previous_pdf,
            current_pdf=current_pdf,
            symbol=symbol,
            company_name=new.company_name,
            status="NEW",
            current_price=new.price,
        )

    # Removed
    for symbol in previous_records.keys() - current_records.keys():

        old = previous_records[symbol]

        ComparisonResult.objects.create(
            previous_pdf=previous_pdf,
            current_pdf=current_pdf,
            symbol=symbol,
            company_name=old.company_name,
            status="REMOVED",
            previous_price=old.price,
        )

    logger.info("Comparison completed successfully.")