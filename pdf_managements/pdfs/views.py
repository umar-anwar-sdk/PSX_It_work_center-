
import os
import logging

from django.shortcuts import render, redirect

from scraper.forms import PDFDocumentForm
from scraper.models import PDFDocument
# from scraper.utils import get_file_hash

logger = logging.getLogger(__name__)



# Create your views here.

def home(request):
    
    return render(request, 'index.html', )


def company_analysis(request):
    return render(request, 'pages/company-analysis.html')


def daily_market_explorer(request):
 
    return render(request, 'pages/daily-market-explorer.html',)


def market_analysis(request):
    return render(request, 'pages/market-analytics.html')


def market_comparison(request):
    return render(request, 'pages/market-comparison.html')


def pdf_management(request):

    if request.method == "POST":

        form = PDFDocumentForm(request.POST, request.FILES)

        if form.is_valid():

            file = request.FILES.get("file")

            if not file:
                pdfs = PDFDocument.objects.order_by("-uploaded_at")
                return render(request, "pages/pdf-management.html", {
                    "form": form,
                    "pdfs": pdfs,
                    "error": "Please select a PDF file."
                })

            # Duplicate Check
            # file_hash = get_file_hash(file)
            # existing_pdf = PDFDocument.objects.filter(
            #     file_hash=file_hash
            # ).first()

            # if existing_pdf:
            #     pdfs = PDFDocument.objects.order_by("-uploaded_at")

            #     return render(request, "pages/pdf-management.html", {
            #         "form": form,
            #         "pdfs": pdfs,
            #         "error": "This PDF already exists.",
            #         "existing_pdf": existing_pdf
            #     })

            pdf = form.save(commit=False)

            # Name automatically set
            pdf.name = os.path.splitext(file.name)[0]

            # pdf.file_hash = file_hash

            pdf.save()

            logger.info(f"PDF Uploaded : {pdf.name}")

            return redirect("pdf_management")

    else:

        form = PDFDocumentForm()

    pdfs = PDFDocument.objects.order_by("-uploaded_at")

    context = {
        "form": form,
        "pdfs": pdfs,
    }

    return render(request, "pages/pdf-management.html", context)


def reports(request):
    return render(request, 'pages/reports.html')


def search_screener(request):
    return render(request, 'pages/search-screener.html')


def settings(request):
    return render(request, 'pages/settings.html')


def watchlist(request):
    return render(request, 'pages/watchlist.html')



