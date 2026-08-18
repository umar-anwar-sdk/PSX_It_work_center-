from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from scraper.models import ComparisonResult, ExtractedCompanyRecord, PDFDocument
from scraper.utils import process_pdf_document

from .models import (
    AlertHistory,
    AlertSetting,
    ClientCompany,
    CompanySettings,
    ModulePermission,
    WatchlistAlertRule,
    WatchlistEntry,
)
from .services import dispatch_market_alerts


class ClientAccountTestCase(TestCase):
    def create_company(self, username="company-a", active=True):
        user = get_user_model().objects.create_user(
            username=username,
            password="secure-test-password",
            email=f"{username}@example.com",
            is_active=active,
        )
        company = ClientCompany.objects.create(user=user, company_name=username.title())
        CompanySettings.objects.create(company=company)
        return user, company

    def grant(self, company, module, **actions):
        defaults = {"can_view": True, **{f"can_{key}": value for key, value in actions.items()}}
        return ModulePermission.objects.update_or_create(company=company, module=module, defaults=defaults)[0]


class AuthenticationPermissionTests(ClientAccountTestCase):
    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('home')}")

    def test_company_direct_url_is_blocked_without_permission(self):
        user, company = self.create_company()
        self.grant(company, "dashboard")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("market-analytics")).status_code, 403)

    def test_permission_enables_page_and_sidebar_link(self):
        user, company = self.create_company()
        self.grant(company, "dashboard")
        self.grant(company, "market_analytics")
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertContains(response, reverse("market-analytics"))

    def test_inactive_company_cannot_login(self):
        self.create_company(active=False)
        response = self.client.post(reverse("login"), {
            "username": "company-a", "password": "secure-test-password"
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_upload_post_requires_action_permission(self):
        user, company = self.create_company()
        self.grant(company, "pdf_management")
        self.client.force_login(user)
        response = self.client.post(reverse("pdf-management"), {
            "file": SimpleUploadedFile("market.pdf", b"%PDF-1.4\n", content_type="application/pdf")
        })
        self.assertEqual(response.status_code, 403)


class WatchlistIsolationTests(ClientAccountTestCase):
    def setUp(self):
        self.user_a, self.company_a = self.create_company("company-a")
        self.user_b, self.company_b = self.create_company("company-b")
        for company in (self.company_a, self.company_b):
            self.grant(company, "watchlist")
            self.grant(company, "watchlist_management", create=True, delete=True)
        pdf = PDFDocument.objects.create(name="latest", file="pdfs/latest.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ExtractedCompanyRecord.objects.create(pdf_document=pdf, symbol="AAA", company_name="Alpha", price=10, change_percent=2, volume=100)

    def test_watchlists_are_isolated_and_duplicates_prevented(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("watchlist"), {"action": "add", "symbol": "aaa"})
        self.client.post(reverse("watchlist"), {"action": "add", "symbol": "AAA"})
        self.assertEqual(WatchlistEntry.objects.filter(user=self.user_a).count(), 1)
        self.assertEqual(WatchlistEntry.objects.filter(user=self.user_b).count(), 0)
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("watchlist"))
        self.assertNotContains(response, "Alpha")

    def test_two_users_can_watch_same_symbol_with_different_thresholds(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("watchlist"), {"action": "add", "symbol": "AAA"})
        self.client.post(reverse("watchlist"), {"action": "save_rules", "symbol": "AAA", "value_threshold": "2", "value_enabled": "on", "email_enabled": "on", "in_app_enabled": "on"})
        self.client.force_login(self.user_b)
        self.client.post(reverse("watchlist"), {"action": "add", "symbol": "AAA"})
        self.client.post(reverse("watchlist"), {"action": "save_rules", "symbol": "AAA", "value_threshold": "5", "value_enabled": "on", "email_enabled": "on", "in_app_enabled": "on"})
        self.assertEqual(WatchlistEntry.objects.filter(symbol="AAA").count(), 2)
        self.assertEqual(WatchlistAlertRule.objects.filter(watchlist_entry__symbol="AAA").count(), 2)
        self.assertEqual(
            WatchlistAlertRule.objects.get(watchlist_entry__user=self.user_a, alert_type=WatchlistAlertRule.VALUE_DIFFERENCE).threshold,
            Decimal("2"),
        )
        self.assertEqual(
            WatchlistAlertRule.objects.get(watchlist_entry__user=self.user_b, alert_type=WatchlistAlertRule.VALUE_DIFFERENCE).threshold,
            Decimal("5"),
        )

    def test_duplicate_market_processing_does_not_create_duplicate_alerts(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("watchlist"), {"action": "add", "symbol": "AAA"})
        self.client.post(reverse("watchlist"), {"action": "save_rules", "symbol": "AAA", "value_threshold": "2", "value_enabled": "on", "email_enabled": "on", "in_app_enabled": "on"})
        previous = PDFDocument.objects.create(name="old", file="pdfs/old.pdf", report_date=date(2026, 7, 31), is_processed=True)
        current = PDFDocument.objects.create(name="new", file="pdfs/new.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ComparisonResult.objects.create(previous_pdf=previous, current_pdf=current, symbol="AAA", company_name="Alpha", status="EXISTING", previous_price=10, current_price=12)
        self.assertEqual(dispatch_market_alerts(current), 1)
        self.assertEqual(AlertHistory.objects.filter(user=self.user_a, symbol="AAA").count(), 1)
        self.assertEqual(dispatch_market_alerts(current), 0)
        self.assertEqual(AlertHistory.objects.filter(user=self.user_a, symbol="AAA").count(), 1)


class AnalyticsFilterTests(ClientAccountTestCase):
    def test_combined_filters_apply_all_conditions(self):
        user, company = self.create_company()
        self.grant(company, "market_analytics")
        self.client.force_login(user)
        pdf = PDFDocument.objects.create(name="latest", file="pdfs/latest.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ExtractedCompanyRecord.objects.create(pdf_document=pdf, symbol="AAA", company_name="Alpha", sector="REFINERY", price=10, change_percent=-2, volume=1000)
        ExtractedCompanyRecord.objects.create(pdf_document=pdf, symbol="BBB", company_name="Beta", sector="REFINERY", price=20, change_percent=3, volume=2000)
        ExtractedCompanyRecord.objects.create(pdf_document=pdf, symbol="CCC", company_name="Gamma", sector="CEMENT", price=5, change_percent=-4, volume=5000)
        response = self.client.get(reverse("market-analytics"), {
            "date": "2026-08-01", "sector": "REFINERY", "trend": "down", "min_volume": "500"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([record.symbol for record in response.context["analytics_records"]], ["AAA"])

    def test_invalid_filters_return_empty_safe_state(self):
        user, company = self.create_company()
        self.grant(company, "market_analytics")
        self.client.force_login(user)
        response = self.client.get(reverse("market-analytics"), {"min_volume": "-1", "trend": "sideways"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_errors"])

    def test_removed_status_uses_previous_session_records(self):
        user, company = self.create_company()
        self.grant(company, "market_analytics")
        self.client.force_login(user)
        previous = PDFDocument.objects.create(name="old", file="pdfs/old.pdf", report_date=date(2026, 7, 31), is_processed=True)
        current = PDFDocument.objects.create(name="new", file="pdfs/new.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ExtractedCompanyRecord.objects.create(pdf_document=previous, symbol="OLD", company_name="Removed Co", sector="CEMENT", price=10, change_percent=-2, volume=1000)
        ComparisonResult.objects.create(previous_pdf=previous, current_pdf=current, symbol="OLD", company_name="Removed Co", status="REMOVED", previous_price=10)
        response = self.client.get(reverse("market-analytics"), {"date": "2026-08-01", "status": "REMOVED"})
        self.assertEqual([record.symbol for record in response.context["analytics_records"]], ["OLD"])


class AlertDeliveryTests(ClientAccountTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_threshold_alert_is_created_and_sent(self):
        _user, company = self.create_company()
        AlertSetting.objects.create(company=company, alert_type=AlertSetting.PRICE_CHANGE, enabled=True, threshold=5)
        previous = PDFDocument.objects.create(name="old", file="pdfs/old.pdf", report_date=date(2026, 7, 31), is_processed=True)
        current = PDFDocument.objects.create(name="new", file="pdfs/new.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ComparisonResult.objects.create(previous_pdf=previous, current_pdf=current, symbol="AAA", company_name="Alpha", status="EXISTING", previous_price=10, current_price=11)
        self.assertEqual(dispatch_market_alerts(current), 1)
        self.assertEqual(AlertHistory.objects.get().email_status, AlertHistory.EMAIL_SENT)

    def test_email_failure_is_preserved_without_crashing(self):
        _user, company = self.create_company()
        AlertSetting.objects.create(company=company, alert_type=AlertSetting.PRICE_CHANGE, enabled=True, threshold=5)
        previous = PDFDocument.objects.create(name="old", file="pdfs/old.pdf", report_date=date(2026, 7, 31), is_processed=True)
        current = PDFDocument.objects.create(name="new", file="pdfs/new.pdf", report_date=date(2026, 8, 1), is_processed=True)
        ComparisonResult.objects.create(previous_pdf=previous, current_pdf=current, symbol="AAA", company_name="Alpha", status="EXISTING", previous_price=10, current_price=11)
        with patch("pdfs.services.send_mail", side_effect=RuntimeError("SMTP unavailable")):
            self.assertEqual(dispatch_market_alerts(current), 1)
        alert = AlertHistory.objects.get()
        self.assertEqual(alert.email_status, AlertHistory.EMAIL_FAILED)
        self.assertEqual(alert.email_error, "RuntimeError: delivery failed")
        self.assertNotIn("SMTP unavailable", alert.email_error)


class ProcessingIntegrityTests(TestCase):
    def test_processing_failure_rolls_back_records_and_processed_state(self):
        document = PDFDocument.objects.create(
            name="atomic-processing",
            file=SimpleUploadedFile("atomic-processing.pdf", b"%PDF-test"),
        )
        self.addCleanup(document.file.storage.delete, document.file.name)
        record = {
            "company_name": "Alpha Limited",
            "symbol": "ALPHA",
            "sector": "CEMENT",
            "price": Decimal("10"),
            "change_value": Decimal("1"),
            "change_percent": Decimal("10"),
            "volume": 100,
        }

        with (
            patch("scraper.utils.extract_report_datetime", return_value=(date(2026, 8, 1), None)),
            patch("scraper.utils.extract_company_information", return_value=[record]),
            patch("scraper.utils.compare_with_previous_pdf", side_effect=RuntimeError("comparison failed")),
        ):
            with self.assertRaises(RuntimeError):
                process_pdf_document(document)

        document.refresh_from_db()
        self.assertFalse(document.is_processed)
        self.assertIsNone(document.report_date)
        self.assertFalse(document.extracted_companies.exists())
