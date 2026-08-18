import logging
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from scraper.models import ComparisonResult

from .models import (
    AlertHistory,
    AlertSetting,
    ClientCompany,
    CompanySettings,
    WatchlistAlertRule,
    WatchlistEntry,
)


logger = logging.getLogger(__name__)


def _create_watchlist_alert(entry, rule, comparison, triggered_value, message, threshold, event_key):
    alert = AlertHistory.objects.create(
        user=entry.user,
        company=entry.company,
        watchlist_entry=entry,
        alert_type=rule.alert_type,
        symbol=entry.symbol,
        message=message,
        triggered_value=triggered_value,
        threshold=threshold,
        email_status=AlertHistory.EMAIL_PENDING,
    )
    if rule.in_app_enabled:
        return alert
    alert.email_status = AlertHistory.EMAIL_SKIPPED
    alert.save(update_fields=["email_status"])
    return alert


def _send_watchlist_email(alert, recipient, message):
    if not recipient:
        alert.email_status = AlertHistory.EMAIL_SKIPPED
        alert.save(update_fields=["email_status"])
        return

    try:
        send_mail(
            subject=f"Watchlist Alert - {alert.symbol}",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
        alert.email_status = AlertHistory.EMAIL_SENT
        alert.sent_at = timezone.now()
        alert.save(update_fields=["email_status", "sent_at"])
    except Exception as exc:
        logger.exception("Watchlist email failed for user %s", alert.user_id)
        alert.email_status = AlertHistory.EMAIL_FAILED
        alert.email_error = f"{type(exc).__name__}: delivery failed"
        alert.save(update_fields=["email_status", "email_error"])


def dispatch_market_alerts(pdf_document):
    """Evaluate both the current user-owned watchlist rules and the legacy company settings."""
    comparisons = list(
        ComparisonResult.objects.filter(current_pdf=pdf_document, status="EXISTING")
        .exclude(previous_price__isnull=True)
        .exclude(current_price__isnull=True)
        .select_related("previous_pdf", "current_pdf")
    )
    if not comparisons:
        return 0

    active_entries = list(
        WatchlistEntry.objects.filter(is_active=True, user__is_active=True)
        .select_related("user", "company", "company__user")
        .prefetch_related("alert_rules")
    )
    entries_by_symbol = defaultdict(list)
    for entry in active_entries:
        entries_by_symbol[entry.symbol.upper()].append(entry)

    created_count = 0
    for comparison in comparisons:
        symbol_entries = entries_by_symbol.get((comparison.symbol or "").upper(), [])
        if not symbol_entries:
            continue

        previous_pdf = comparison.previous_pdf
        previous_date = previous_pdf.report_date if previous_pdf and previous_pdf.report_date else None
        current_date = pdf_document.report_date if pdf_document.report_date else None
        comparison_date_delta = None
        if previous_date and current_date:
            comparison_date_delta = (current_date - previous_date).days

        for entry in symbol_entries:
            if entry.user is None or not entry.user.email:
                continue
            for rule in entry.alert_rules.filter(is_active=True):
                if rule.alert_type == WatchlistAlertRule.VALUE_DIFFERENCE:
                    if comparison.previous_price in (None, Decimal("0")) or comparison.current_price is None:
                        continue
                    difference = abs(comparison.current_price - comparison.previous_price)
                    threshold = rule.threshold
                    if difference < threshold:
                        continue
                    event_key = f"value:{comparison.current_pdf_id}:{comparison.symbol}:{entry.pk}:{rule.pk}"
                    if rule.last_triggered_event_key == event_key:
                        continue
                    message = (
                        f"{comparison.symbol} ({entry.company_name or comparison.company_name}) moved from "
                        f"PKR {comparison.previous_price:.2f} to PKR {comparison.current_price:.2f}. "
                        f"Difference detected: {difference:.2f}. Threshold: {threshold:.2f}."
                    )
                    alert = _create_watchlist_alert(
                        entry,
                        rule,
                        comparison,
                        difference,
                        message,
                        threshold,
                        event_key,
                    )
                    if rule.email_enabled:
                        _send_watchlist_email(alert, entry.user.email.strip(), message)
                    rule.last_triggered_event_key = event_key
                    rule.last_triggered_at = timezone.now()
                    rule.last_triggered_value = difference
                    rule.save(update_fields=["last_triggered_event_key", "last_triggered_at", "last_triggered_value", "updated_at"])
                    created_count += 1

                elif rule.alert_type == WatchlistAlertRule.DATE_OCCURRENCE:
                    if previous_date is None or current_date is None:
                        continue
                    if comparison_date_delta is None or comparison_date_delta < rule.occurrence_gap_days:
                        continue
                    event_key = f"date:{comparison.current_pdf_id}:{comparison.symbol}:{entry.pk}:{rule.pk}"
                    if rule.last_triggered_event_key == event_key:
                        continue
                    message = (
                        f"{comparison.symbol} ({entry.company_name or comparison.company_name}) appeared again after "
                        f"{comparison_date_delta} day(s). Configured occurrence gap: {rule.occurrence_gap_days} day(s)."
                    )
                    alert = _create_watchlist_alert(
                        entry,
                        rule,
                        comparison,
                        Decimal(comparison_date_delta),
                        message,
                        Decimal(rule.occurrence_gap_days),
                        event_key,
                    )
                    if rule.email_enabled:
                        _send_watchlist_email(alert, entry.user.email.strip(), message)
                    rule.last_triggered_event_key = event_key
                    rule.last_triggered_at = timezone.now()
                    rule.last_triggered_value = Decimal(comparison_date_delta)
                    rule.save(update_fields=["last_triggered_event_key", "last_triggered_at", "last_triggered_value", "updated_at"])
                    created_count += 1

    created_count += dispatch_company_alerts(pdf_document)
    return created_count


def dispatch_company_alerts(pdf_document):
    """Backward-compatible company-wide alert dispatch used by the legacy settings model."""
    comparisons = list(
        ComparisonResult.objects.filter(current_pdf=pdf_document, status="EXISTING")
        .exclude(previous_price__isnull=True)
        .exclude(current_price__isnull=True)
    )
    if not comparisons:
        return 0

    created_count = 0
    for company in ClientCompany.objects.filter(user__is_active=True).select_related("user"):
        try:
            company_settings = company.settings
        except CompanySettings.DoesNotExist:
            continue

        settings_by_type = {item.alert_type: item for item in company.alert_settings.filter(enabled=True)}
        price_setting = settings_by_type.get(AlertSetting.PRICE_CHANGE)
        watchlist_setting = settings_by_type.get(AlertSetting.WATCHLIST_MOVEMENT)
        watchlist_symbols = set(WatchlistEntry.objects.filter(company=company, is_active=True).values_list("symbol", flat=True))
        volume_setting = settings_by_type.get(AlertSetting.VOLUME_MOVEMENT)
        previous_pdf = comparisons[0].previous_pdf if comparisons else None
        previous_volumes = {item.symbol: item.volume for item in previous_pdf.extracted_companies.only("symbol", "volume")} if previous_pdf else {}
        current_volumes = {item.symbol: item.volume for item in pdf_document.extracted_companies.only("symbol", "volume")}

        for comparison in comparisons:
            previous_price = comparison.previous_price
            current_price = comparison.current_price
            if previous_price in (None, Decimal("0")) or current_price is None:
                continue
            change_percent = ((current_price - previous_price) / previous_price) * Decimal("100")
            setting = None
            alert_type = None
            if (
                watchlist_setting
                and company_settings.watchlist_alerts
                and comparison.symbol in watchlist_symbols
                and abs(change_percent) >= watchlist_setting.threshold
            ):
                setting = watchlist_setting
                alert_type = AlertSetting.WATCHLIST_MOVEMENT
            elif price_setting and abs(change_percent) >= price_setting.threshold:
                setting = price_setting
                alert_type = AlertSetting.PRICE_CHANGE
            elif volume_setting:
                previous_volume = previous_volumes.get(comparison.symbol)
                current_volume = current_volumes.get(comparison.symbol)
                if previous_volume not in (None, 0) and current_volume is not None:
                    volume_change = ((Decimal(current_volume) - Decimal(previous_volume)) / Decimal(previous_volume)) * Decimal("100")
                    if abs(volume_change) >= volume_setting.threshold:
                        setting = volume_setting
                        alert_type = AlertSetting.VOLUME_MOVEMENT
                        change_percent = volume_change
            if setting is None:
                continue

            message = (
                f"{comparison.symbol} ({comparison.company_name}) moved from "
                f"PKR {previous_price:.2f} to PKR {current_price:.2f} "
                f"({change_percent:+.2f}%)."
            ) if alert_type != AlertSetting.VOLUME_MOVEMENT else (
                f"{comparison.symbol} ({comparison.company_name}) volume moved from "
                f"{previous_volumes.get(comparison.symbol):,} to {current_volumes.get(comparison.symbol):,} "
                f"({change_percent:+.2f}%)."
            )
            alert = AlertHistory.objects.create(
                user=company.user,
                company=company,
                alert_type=alert_type,
                symbol=comparison.symbol,
                message=message,
                triggered_value=change_percent,
                threshold=setting.threshold,
                email_status=AlertHistory.EMAIL_PENDING,
            )
            created_count += 1
            recipient = company.user.email.strip()
            if not company_settings.email_notifications or not recipient:
                alert.email_status = AlertHistory.EMAIL_SKIPPED
                alert.save(update_fields=["email_status"])
                continue
            try:
                send_mail(
                    subject=f"PSX Market Alert - {comparison.symbol}",
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient],
                    fail_silently=False,
                )
                alert.email_status = AlertHistory.EMAIL_SENT
                alert.sent_at = timezone.now()
                alert.save(update_fields=["email_status", "sent_at"])
            except Exception as exc:
                logger.exception("Alert email failed for client company %s", company.pk)
                alert.email_status = AlertHistory.EMAIL_FAILED
                alert.email_error = f"{type(exc).__name__}: delivery failed"
                alert.save(update_fields=["email_status", "email_error"])
    return created_count
