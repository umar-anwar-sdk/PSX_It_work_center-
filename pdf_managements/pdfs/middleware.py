from django.conf import settings


class SecurityHeadersMiddleware:
    """Set browser hardening headers without relying on a specific edge proxy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        scripts = "'self' 'unsafe-inline'" if request.path.startswith("/admin/") else "'self'"
        directives = [
            "default-src 'self'",
            f"script-src {scripts}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
        if not settings.DEBUG:
            directives.append("upgrade-insecure-requests")
        response.setdefault("Content-Security-Policy", "; ".join(directives))
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        if request.path.startswith("/accounts/") or getattr(request.user, "is_authenticated", False):
            response.setdefault("Cache-Control", "no-store, private")
        return response
