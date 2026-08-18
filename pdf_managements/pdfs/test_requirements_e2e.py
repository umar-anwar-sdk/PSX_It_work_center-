from datetime import date, time
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfWriter

from scraper.forms import PDFDocumentForm
from scraper.models import ComparisonResult, ExtractedCompanyRecord, GeneratedReport, PDFDocument
from scraper.utils import compare_with_previous_pdf

from .models import ClientCompany, CompanySettings, ModulePermission


class RequirementFlowTestCase(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="requirements-admin",
            password="strong-admin-password",
            email="admin@example.com",
            is_staff=True,
        )
        self.client.force_login(self.admin)

    def create_pdf(self, name, report_date, report_time=time(9, 0), processed=True):
        return PDFDocument.objects.create(
            name=name,
            file=SimpleUploadedFile(f"{name}.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
            report_date=report_date,
            report_time=report_time,
            is_processed=processed,
        )

    def create_record(self, pdf, symbol, company, *, sector="CEMENT", price="10", change="0", volume=100):
        return ExtractedCompanyRecord.objects.create(
            pdf_document=pdf,
            symbol=symbol,
            company_name=company,
            sector=sector,
            price=Decimal(price),
            change_percent=Decimal(change),
            volume=volume,
        )


class AuthenticationEndToEndTests(RequirementFlowTestCase):
    def test_admin_login_invalid_login_and_logout(self):
        self.client.logout()
        invalid = self.client.post(reverse("login"), {"username": "requirements-admin", "password": "wrong"})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Invalid username/password")
        valid = self.client.post(
            reverse("login"),
            {"username": "requirements-admin", "password": "strong-admin-password"},
        )
        self.assertRedirects(valid, reverse("home"))
        logout = self.client.post(reverse("logout"))
        self.assertRedirects(logout, reverse("login"))

    def test_disabled_company_session_loses_backend_access(self):
        user = get_user_model().objects.create_user(username="disabled-client", password="safe-password")
        company = ClientCompany.objects.create(user=user, company_name="Disabled Client")
        CompanySettings.objects.create(company=company)
        ModulePermission.objects.create(company=company, module="dashboard", can_view=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertRedirects(self.client.get(reverse("home")), f"{reverse('login')}?next={reverse('home')}")


class ExplorerAndHistoryTests(RequirementFlowTestCase):
    def test_invalid_and_missing_dates_do_not_silently_show_latest_data(self):
        latest = self.create_pdf("latest", date(2026, 8, 1))
        self.create_record(latest, "AAA", "Alpha")
        invalid = self.client.get(reverse("daily-market-explorer"), {"date": "not-a-date"})
        self.assertIsNone(invalid.context["selected_pdf"])
        self.assertTrue(invalid.context["invalid_date"])
        missing = self.client.get(reverse("daily-market-explorer"), {"date": "2099-01-01"})
        self.assertIsNone(missing.context["selected_pdf"])
        self.assertContains(missing, "No market data is available")

    def test_date_wise_history_uses_one_latest_report_per_market_date(self):
        old = self.create_pdf("old", date(2026, 7, 31))
        same_day_early = self.create_pdf("same-early", date(2026, 8, 1), time(9, 0))
        same_day_latest = self.create_pdf("same-latest", date(2026, 8, 1), time(10, 0))
        self.create_record(old, "OLD", "Old Co", volume=50)
        self.create_record(same_day_early, "AAA", "Alpha Early", volume=100)
        self.create_record(same_day_latest, "AAA", "Alpha", volume=250)
        response = self.client.get(reverse("daily-market-explorer"), {"status": "history"})
        self.assertEqual([row.name for row in response.context["history_rows"]], ["same-latest", "old"])
        self.assertContains(response, "Date Wise History")

    def test_comparison_identifies_new_removed_and_existing(self):
        previous = self.create_pdf("previous", date(2026, 7, 31))
        current = self.create_pdf("current", date(2026, 8, 1))
        self.create_record(previous, "KEEP", "Keep Previous", price="10")
        self.create_record(previous, "GONE", "Gone", price="5")
        self.create_record(current, "KEEP", "Keep Current", price="11")
        self.create_record(current, "NEW", "New", price="7")
        compare_with_previous_pdf(current)
        statuses = dict(ComparisonResult.objects.filter(current_pdf=current).values_list("symbol", "status"))
        self.assertEqual(statuses, {"KEEP": "EXISTING", "GONE": "REMOVED", "NEW": "NEW"})


class CompanyAnalysisRequirementTests(RequirementFlowTestCase):
    def test_search_respects_selected_date_and_history_deduplicates_same_date(self):
        old = self.create_pdf("old", date(2026, 7, 31))
        same_day_early = self.create_pdf("early", date(2026, 8, 1), time(9, 0))
        same_day_latest = self.create_pdf("latest", date(2026, 8, 1), time(10, 0))
        self.create_record(old, "AAA", "Alpha Old", price="9")
        self.create_record(same_day_early, "AAA", "Alpha Early", price="10")
        latest_record = self.create_record(same_day_latest, "AAA", "Alpha Latest", price="11")
        selected = self.client.get(reverse("company-analysis"), {"date": "2026-08-01", "q": "AAA"})
        self.assertEqual(selected.context["company"], latest_record)
        self.assertEqual(selected.context["history_count"], 2)
        self.assertEqual(selected.context["previous_record"].pdf_document, old)
        absent = self.client.get(reverse("company-analysis"), {"date": "2026-07-31", "q": "MISSING"})
        self.assertTrue(absent.context["result_not_found"])


class AnalyticsRequirementTests(RequirementFlowTestCase):
    def test_date_range_and_market_filters_are_combined(self):
        old = self.create_pdf("old", date(2026, 7, 1))
        current = self.create_pdf("current", date(2026, 8, 1))
        self.create_record(old, "AAA", "Alpha Old", sector="REFINERY", change="-3", volume=100)
        self.create_record(current, "AAA", "Alpha", sector="REFINERY", change="-2", volume=1000)
        self.create_record(current, "BBB", "Beta", sector="REFINERY", change="4", volume=2000)
        response = self.client.get(reverse("market-analytics"), {
            "date_from": "2026-07-15",
            "date_to": "2026-08-01",
            "sector": "REFINERY",
            "trend": "down",
            "min_volume": "500",
        })
        self.assertEqual([record.symbol for record in response.context["analytics_records"]], ["AAA"])
        self.assertEqual(response.context["filters"]["date_from"], "2026-07-15")

    def test_invalid_date_range_returns_safe_empty_state(self):
        current = self.create_pdf("current", date(2026, 8, 1))
        self.create_record(current, "AAA", "Alpha")
        response = self.client.get(reverse("market-analytics"), {
            "date_from": "2026-08-02", "date_to": "2026-08-01"
        })
        self.assertTrue(response.context["invalid_date"])
        self.assertIn("Start date must be on or before end date.", response.context["filter_errors"])
        self.assertEqual(response.context["total_stocks"], 0)


class UploadAndReportRequirementTests(RequirementFlowTestCase):
    def test_upload_form_rejects_empty_wrong_extension_and_fake_pdf(self):
        empty = PDFDocumentForm(files={"file": SimpleUploadedFile("empty.pdf", b"")})
        wrong = PDFDocumentForm(files={"file": SimpleUploadedFile("market.txt", b"hello")})
        fake = PDFDocumentForm(files={"file": SimpleUploadedFile("market.pdf", b"not-pdf")})
        self.assertFalse(empty.is_valid())
        self.assertFalse(wrong.is_valid())
        self.assertFalse(fake.is_valid())

    @patch("pdfs.views.process_pdf_document")
    def test_duplicate_upload_is_prevented_by_file_hash(self, mocked_process):
        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(buffer)
        payload = buffer.getvalue()
        first = self.client.post(reverse("pdf-management"), {
            "file": SimpleUploadedFile("market.pdf", payload, content_type="application/pdf")
        })
        second = self.client.post(reverse("pdf-management"), {
            "file": SimpleUploadedFile("market-copy.pdf", payload, content_type="application/pdf")
        })
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(PDFDocument.objects.count(), 1)
        self.assertEqual(mocked_process.call_count, 1)

    def test_generated_reports_are_isolated_between_company_accounts(self):
        user_a = get_user_model().objects.create_user(username="report-a", password="safe-password")
        user_b = get_user_model().objects.create_user(username="report-b", password="safe-password")
        company_a = ClientCompany.objects.create(user=user_a, company_name="Report A")
        company_b = ClientCompany.objects.create(user=user_b, company_name="Report B")
        for company in (company_a, company_b):
            CompanySettings.objects.create(company=company)
            ModulePermission.objects.create(company=company, module="reports", can_view=True, can_export=True)
        report = GeneratedReport.objects.create(
            report_type="daily",
            name="Private report",
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 1),
            created_by=user_a,
            file=SimpleUploadedFile("private.pdf", b"%PDF-1.4\n%%EOF"),
        )
        self.client.force_login(user_b)
        self.assertEqual(self.client.get(reverse("download-report", args=[report.pk])).status_code, 404)


class ScreenerAndPaginationTests(RequirementFlowTestCase):
    def test_invalid_numeric_filter_returns_no_misleading_results(self):
        current = self.create_pdf("current", date(2026, 8, 1))
        self.create_record(current, "AAA", "Alpha")
        response = self.client.get(reverse("search-screener"), {"min_price": "broken"})
        self.assertEqual(response.context["result_count"], 0)

    def test_filter_query_is_preserved_for_pagination(self):
        current = self.create_pdf("current", date(2026, 8, 1))
        for index in range(25):
            self.create_record(current, f"A{index:02}", f"Alpha {index}", sector="CEMENT")
        response = self.client.get(reverse("market-analytics"), {"sector": "CEMENT", "page": "2"})
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertIn("sector=CEMENT", response.context["filter_querystring"])
