from django.shortcuts import render


def bad_request(request, exception=None):
    return render(request, "errors/error.html", {"code": 400, "message": "The request could not be processed."}, status=400)


def permission_denied(request, exception=None):
    return render(request, "errors/error.html", {"code": 403, "message": "You do not have permission to access this resource."}, status=403)


def page_not_found(request, exception=None):
    return render(request, "errors/error.html", {"code": 404, "message": "The requested page was not found."}, status=404)


def server_error(request):
    return render(request, "errors/error.html", {"code": 500, "message": "An unexpected error occurred. Please try again later."}, status=500)
