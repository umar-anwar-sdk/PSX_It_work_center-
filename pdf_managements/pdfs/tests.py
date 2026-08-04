import json
from datetime import date, time
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from scraper.models import ComparisonResult, ExtractedCompanyRecord, GeneratedReport, PDFDocument

from .models import ScrapedRecord
from .views import import_data_from_folder


class ScraperImportTests(TestCase):
    def test_imports_json_records_from_folder(self):
        folder = Path(__file__).resolve().parent / 'testdata' / 'scrapper'
        folder.mkdir(parents=True, exist_ok=True)
        sample_file = folder / 'sample.json'
        sample_file.write_text(
            json.dumps([
                {
                    'symbol': 'CNERGY',
                    'company': 'Cnergyico PK Limited',
                    'sector': 'REFINERY',
                    'price': '9.40',
                    'change_percent': '+7.80%',
                    'volume': '211683514',
                    'trend': 'Up',
                    'date': '2026-07-09',
                },
                {
                    'symbol': 'LSECL',
                    'company': 'LSE Capital Limited',
                    'sector': 'INV. BANKS / SECURITIES',
                    'price': '7.84',
                    'change_percent': '+14.62%',
                    'volume': '58382446',
                    'trend': 'Up',
                    'date': '2026-07-09',
                },
            ]),
            encoding='utf-8',
        )

        imported_count = import_data_from_folder(folder)

        self.assertEqual(imported_count, 2)
        self.assertEqual(ScrapedRecord.objects.count(), 2)
        self.assertEqual(ScrapedRecord.objects.get(symbol='CNERGY').company, 'Cnergyico PK Limited')


class DailyMarketExplorerViewTests(TestCase):
    def setUp(self):
        self.pdf_18 = self._create_pdf("pdf-18", date(2026, 7, 18), time(9, 0))
        self.pdf_19 = self._create_pdf("pdf-19", date(2026, 7, 19), time(9, 0))
        self.pdf_19_later = self._create_pdf("pdf-19-later", date(2026, 7, 19), time(10, 0))

        self._create_company(self.pdf_18, "A", "Alpha", 1000)
        self._create_company(self.pdf_18, "B", "Beta", 500)
        self._create_company(self.pdf_19, "A", "Alpha", 1200)
        self._create_company(self.pdf_19, "C", "Gamma", 2000)
        self._create_company(self.pdf_19_later, "A", "Alpha", 1200)
        self._create_company(self.pdf_19_later, "C", "Gamma", 2000)

        ComparisonResult.objects.create(
            previous_pdf=self.pdf_18,
            current_pdf=self.pdf_19,
            symbol="A",
            company_name="Alpha",
            status="EXISTING",
            previous_price=1000,
            current_price=1200,
        )
        ComparisonResult.objects.create(
            previous_pdf=self.pdf_18,
            current_pdf=self.pdf_19,
            symbol="C",
            company_name="Gamma",
            status="NEW",
            current_price=2000,
        )
        ComparisonResult.objects.create(
            previous_pdf=self.pdf_18,
            current_pdf=self.pdf_19,
            symbol="B",
            company_name="Beta",
            status="REMOVED",
            previous_price=500,
        )

    def _create_pdf(self, name, report_date, report_time):
        return PDFDocument.objects.create(
            name=name,
            file=SimpleUploadedFile(f"{name}.pdf", b"pdf-content", content_type="application/pdf"),
            report_date=report_date,
            report_time=report_time,
        )

    def _create_company(self, pdf_document, symbol, company_name, volume):
        return ExtractedCompanyRecord.objects.create(
            pdf_document=pdf_document,
            company_name=company_name,
            symbol=symbol,
            price=10,
            change_value=1,
            change_percent=1,
            volume=volume,
        )

    def test_latest_pdf_is_selected_by_date_and_upload_time(self):
        response = self.client.get(reverse("daily-market-explorer"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_pdf"], self.pdf_19_later)
        self.assertEqual(response.context["selected_date"], date(2026, 7, 19))
        self.assertEqual(response.context["total_companies"], 2)
        self.assertEqual(response.context["top50_count"], 2)
        self.assertEqual(response.context["new_count"], 1)
        self.assertEqual(response.context["removed_count"], 1)
        self.assertEqual(response.context["existing_count"], 1)

    def test_filter_param_returns_matching_companies(self):
        response = self.client.get(reverse("daily-market-explorer"), {"filter": "new"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["companies"].values_list("symbol", flat=True)), ["C"])


class CompanyAnalysisViewTests(TestCase):
    def setUp(self):
        self.pdf_18 = self._create_pdf("pdf-18", date(2026, 7, 18), time(9, 0))
        self.pdf_19 = self._create_pdf("pdf-19", date(2026, 7, 19), time(9, 0))
        self._create_company(self.pdf_18, "A", "Alpha", 1000)
        self._create_company(self.pdf_19, "B", "Beta", 500)

    def _create_pdf(self, name, report_date, report_time):
        return PDFDocument.objects.create(
            name=name,
            file=SimpleUploadedFile(f"{name}.pdf", b"pdf-content", content_type="application/pdf"),
            report_date=report_date,
            report_time=report_time,
            is_processed=True,
        )

    def _create_company(self, pdf_document, symbol, company_name, volume):
        return ExtractedCompanyRecord.objects.create(
            pdf_document=pdf_document,
            company_name=company_name,
            symbol=symbol,
            price=10,
            change_value=1,
            change_percent=1,
            volume=volume,
        )

    def test_date_dropdown_is_populated_and_selected(self):
        response = self.client.get(reverse("company-analysis"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_date"], date(2026, 7, 19))
        self.assertEqual(response.context["available_dates"], [date(2026, 7, 19), date(2026, 7, 18)])
        self.assertEqual(response.context["company"].pdf_document, self.pdf_19)

    def test_date_filter_uses_selected_report_date(self):
        response = self.client.get(reverse("company-analysis"), {"date": "2026-07-18"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_date"], date(2026, 7, 18))
        self.assertEqual(response.context["company"].pdf_document, self.pdf_18)


class ReportsViewTests(TestCase):
    def test_generates_and_downloads_a_selected_report(self):
        pdf = PDFDocument.objects.create(
            name="daily-data",
            file=SimpleUploadedFile("daily-data.pdf", b"pdf-content", content_type="application/pdf"),
            report_date=date(2026, 7, 19),
            is_processed=True,
        )
        ExtractedCompanyRecord.objects.create(
            pdf_document=pdf,
            company_name="Alpha",
            symbol="ALPHA",
            price=10,
            volume=100,
        )

        response = self.client.post(reverse("reports"), {"report_type": "weekly"})

        self.assertRedirects(response, f"{reverse('reports')}?type=weekly")
        report = GeneratedReport.objects.get()
        self.assertEqual(report.report_type, "weekly")
        download = self.client.get(reverse("download-report", args=[report.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content)[:8], b"%PDF-1.4")
