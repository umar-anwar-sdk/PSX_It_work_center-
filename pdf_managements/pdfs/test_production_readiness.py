from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter

from scraper.forms import PDFDocumentForm


def pdf_payload(*, active=False, pages=1):
    buffer = BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if active:
        writer.add_js("app.alert('unsafe')")
    writer.write(buffer)
    return buffer.getvalue()


class ProductionReadinessTests(TestCase):
    def test_health_endpoints_are_minimal_and_ready(self):
        live = self.client.get(reverse("health-live"))
        ready = self.client.get(reverse("health-ready"))
        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(live.json(), {"status": "ok"})
        self.assertEqual(ready.json(), {"status": "ok"})

    def test_security_headers_block_scripts_and_framing(self):
        response = self.client.get(reverse("login"))
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])

    def test_active_pdf_content_is_rejected(self):
        form = PDFDocumentForm(files={
            "file": SimpleUploadedFile("active.pdf", pdf_payload(active=True), content_type="application/pdf")
        })
        self.assertFalse(form.is_valid())
        self.assertIn("scripts", str(form.errors).lower())

    @override_settings(PDF_UPLOAD_MAX_PAGES=1)
    def test_pdf_page_limit_is_enforced(self):
        form = PDFDocumentForm(files={
            "file": SimpleUploadedFile("large.pdf", pdf_payload(pages=2), content_type="application/pdf")
        })
        self.assertFalse(form.is_valid())
        self.assertIn("at most 1 pages", str(form.errors))

    def test_new_passwords_use_argon2(self):
        user = get_user_model().objects.create_user(username="argon-user", password="safe-test-password")
        self.assertTrue(user.password.startswith("argon2$"))

    @override_settings(AXES_FAILURE_LIMIT=2)
    def test_repeated_login_failures_lock_the_account_and_ip_pair(self):
        get_user_model().objects.create_user(username="locked-user", password="safe-test-password")
        credentials = {"username": "locked-user", "password": "wrong-password"}
        self.client.post(reverse("login"), credentials)
        self.client.post(reverse("login"), credentials)
        response = self.client.post(reverse("login"), {
            "username": "locked-user", "password": "safe-test-password"
        })
        self.assertIn(response.status_code, {403, 429})
        self.assertNotIn("_auth_user_id", self.client.session)
