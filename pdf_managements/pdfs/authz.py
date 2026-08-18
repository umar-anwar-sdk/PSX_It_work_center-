from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import ModulePermission


def get_client_company(user):
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        return user.client_company
    except AttributeError:
        return None


def has_module_permission(user, module, action="view"):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser or user.is_staff:
        return True
    company = get_client_company(user)
    if company is None:
        return False
    field = f"can_{action}"
    if field not in {"can_view", "can_create", "can_edit", "can_delete", "can_export"}:
        return False
    return ModulePermission.objects.filter(company=company, module=module, **{field: True}).exists()


def require_module_permission(request, module, action="view"):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not has_module_permission(request.user, module, action):
        raise PermissionDenied("You do not have permission to access this feature.")
    return None


def module_permission_required(module, action="view"):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            denial = require_module_permission(request, module, action)
            if denial:
                return denial
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied("Administrator access is required.")
        return view_func(request, *args, **kwargs)

    return wrapped
