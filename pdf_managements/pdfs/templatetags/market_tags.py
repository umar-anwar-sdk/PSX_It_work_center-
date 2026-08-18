from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


@register.filter
def compact_number(value):
    """Keep large market figures readable inside compact UI components."""
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return value

    absolute = abs(number)
    for divisor, suffix in (
        (Decimal("1000000000000"), "T"),
        (Decimal("1000000000"), "B"),
        (Decimal("1000000"), "M"),
        (Decimal("1000"), "K"),
    ):
        if absolute >= divisor:
            scaled = number / divisor
            precision = 1 if abs(scaled) >= 100 else 2
            return f"{scaled:.{precision}f}".rstrip("0").rstrip(".") + suffix
    return f"{number:,.0f}"
