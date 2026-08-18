from .authz import get_client_company
from .models import MODULE_CHOICES


def navigation_permissions(request):
    user = getattr(request, "user", None)
    allowed_modules = set()
    company = None
    if user and user.is_authenticated and user.is_active:
        if user.is_staff or user.is_superuser:
            allowed_modules = {code for code, _label in MODULE_CHOICES}
        else:
            company = get_client_company(user)
            if company:
                allowed_modules = set(
                    company.module_permissions.filter(can_view=True).values_list("module", flat=True)
                )
    return {"allowed_modules": allowed_modules, "current_client_company": company}
