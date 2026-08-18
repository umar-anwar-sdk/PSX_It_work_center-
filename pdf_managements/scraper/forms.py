from django import forms
from django.conf import settings
from django.utils.text import get_valid_filename
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .models import PDFDocument


ACTIVE_PDF_KEYS = {
    "/AA", "/EmbeddedFiles", "/JavaScript", "/JS", "/Launch",
    "/OpenAction", "/RichMedia", "/XFA",
}


def _contains_active_content(value, seen=None, depth=0):
    """Fail closed when a PDF object tree contains executable/embedded content."""
    if seen is None:
        seen = set()
    if depth > 30 or len(seen) > 20_000:
        return True
    try:
        value = value.get_object()
    except AttributeError:
        pass
    except Exception:
        return True
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in ACTIVE_PDF_KEYS or str(child) == "/EmbeddedFile":
                return True
            if _contains_active_content(child, seen, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_active_content(child, seen, depth + 1) for child in value)
    return False

class PDFDocumentForm(forms.ModelForm):
    class Meta:
        model = PDFDocument
        fields = ['file']

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        if uploaded_file.size <= 0:
            raise forms.ValidationError("The uploaded PDF is empty.")
        if uploaded_file.size > settings.PDF_UPLOAD_MAX_BYTES:
            max_mb = settings.PDF_UPLOAD_MAX_BYTES // (1024 * 1024)
            raise forms.ValidationError(f"PDF files must be {max_mb} MB or smaller.")
        if not uploaded_file.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Only PDF files are allowed.")
        header = uploaded_file.read(5)
        uploaded_file.seek(0)
        if header != b"%PDF-":
            raise forms.ValidationError("The selected file is not a valid PDF.")
        try:
            reader = PdfReader(uploaded_file, strict=True)
            if reader.is_encrypted:
                raise forms.ValidationError("Encrypted or password-protected PDFs are not accepted.")
            if not reader.pages:
                raise forms.ValidationError("The PDF does not contain any pages.")
            if len(reader.pages) > settings.PDF_UPLOAD_MAX_PAGES:
                raise forms.ValidationError(
                    f"PDF files may contain at most {settings.PDF_UPLOAD_MAX_PAGES} pages."
                )
            if _contains_active_content(reader.trailer.get("/Root", {})):
                raise forms.ValidationError(
                    "PDFs containing scripts, launch actions, forms, or embedded files are not accepted."
                )
        except forms.ValidationError:
            raise
        except (PdfReadError, ValueError, TypeError, OSError) as exc:
            raise forms.ValidationError("The selected file is malformed or unsafe.") from exc
        finally:
            uploaded_file.seek(0)
        uploaded_file.name = get_valid_filename(uploaded_file.name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        return uploaded_file
