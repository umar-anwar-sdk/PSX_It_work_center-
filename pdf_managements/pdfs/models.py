from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class ScrapedRecord(models.Model):
    symbol = models.CharField(max_length=50)
    company = models.CharField(max_length=255)
    sector = models.CharField(max_length=255, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_percent = models.CharField(max_length=50, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    trend = models.CharField(max_length=50, blank=True)
    date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company"]

    def __str__(self):
        return self.company


MODULE_CHOICES = (
    ("dashboard", "Dashboard"),
    ("pdf_management", "PDF Management"),
    ("pdf_upload", "PDF Upload"),
    ("pdf_processing", "PDF Processing"),
    ("daily_market_explorer", "Daily Market Explorer"),
    ("company_analysis", "Company Analysis"),
    ("market_comparison", "Market Comparison"),
    ("market_analytics", "Market Analytics"),
    ("reports", "Reports"),
    ("watchlist", "Watchlist"),
    ("watchlist_management", "Watchlist Management"),
    ("search_screener", "Search & Screener"),
    ("alerts", "Alerts"),
    ("settings_profile", "Settings / Profile"),
)


class ClientCompany(models.Model):
    """A SaaS client account, deliberately separate from PSX-listed companies."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_company",
    )
    company_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company_name"]
        indexes = [models.Index(fields=["company_name"])]

    @property
    def is_active(self):
        return self.user.is_active

    def __str__(self):
        return self.company_name


class ModulePermission(models.Model):
    company = models.ForeignKey(
        ClientCompany,
        on_delete=models.CASCADE,
        related_name="module_permissions",
    )
    module = models.CharField(max_length=40, choices=MODULE_CHOICES)
    can_view = models.BooleanField(default=False)
    can_create = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "module"], name="unique_company_module_permission"),
        ]
        indexes = [models.Index(fields=["company", "module"])]

    def __str__(self):
        return f"{self.company} - {self.get_module_display()}"


class CompanySettings(models.Model):
    company = models.OneToOneField(
        ClientCompany,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    email_notifications = models.BooleanField(default=True)
    watchlist_alerts = models.BooleanField(default=True)
    default_market = models.CharField(max_length=80, default="PSX - Pakistan Stock Exchange")
    timezone = models.CharField(max_length=50, default="Asia/Karachi")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Settings for {self.company}"


class AlertSetting(models.Model):
    PRICE_CHANGE = "price_change"
    WATCHLIST_MOVEMENT = "watchlist_movement"
    VOLUME_MOVEMENT = "volume_movement"
    ALERT_TYPES = (
        (PRICE_CHANGE, "Price change"),
        (WATCHLIST_MOVEMENT, "Watchlist movement"),
        (VOLUME_MOVEMENT, "Volume movement"),
    )

    company = models.ForeignKey(ClientCompany, on_delete=models.CASCADE, related_name="alert_settings")
    alert_type = models.CharField(max_length=40, choices=ALERT_TYPES)
    enabled = models.BooleanField(default=False)
    threshold = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "alert_type"], name="unique_company_alert_setting"),
        ]

    def __str__(self):
        return f"{self.company} - {self.get_alert_type_display()}"


class WatchlistEntry(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="watchlist_entries",
        null=True,
        blank=True,
    )
    company = models.ForeignKey(
        ClientCompany,
        on_delete=models.CASCADE,
        related_name="watchlist_entries",
    )
    symbol = models.CharField(max_length=50)
    company_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "symbol"],
                condition=models.Q(is_active=True),
                name="unique_user_active_watchlist_symbol",
            ),
            models.UniqueConstraint(fields=["company", "symbol"], name="unique_company_watchlist_symbol"),
        ]
        indexes = [models.Index(fields=["user", "symbol"]), models.Index(fields=["company", "symbol"]) ]

    def save(self, *args, **kwargs):
        self.symbol = (self.symbol or "").strip().upper()
        if self.user is None and self.company_id:
            self.user = self.company.user
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user or self.company}: {self.symbol}"


class WatchlistAlertRule(models.Model):
    VALUE_DIFFERENCE = "value_difference"
    DATE_OCCURRENCE = "date_occurrence"
    ALERT_TYPES = (
        (VALUE_DIFFERENCE, "Value difference"),
        (DATE_OCCURRENCE, "Date occurrence"),
    )

    watchlist_entry = models.ForeignKey(
        WatchlistEntry,
        on_delete=models.CASCADE,
        related_name="alert_rules",
    )
    alert_type = models.CharField(max_length=40, choices=ALERT_TYPES)
    threshold = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("2.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    occurrence_gap_days = models.PositiveIntegerField(default=3)
    comparison_type = models.CharField(
        max_length=20,
        choices=[("gte", "Greater than or equal"), ("absolute", "Absolute difference")],
        default="absolute",
    )
    is_active = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    in_app_enabled = models.BooleanField(default=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    last_triggered_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    last_triggered_event_key = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["alert_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["watchlist_entry", "alert_type"],
                name="unique_watchlist_entry_alert_type",
            ),
        ]

    def __str__(self):
        return f"{self.watchlist_entry} - {self.get_alert_type_display()}"


class AlertHistory(models.Model):
    EMAIL_PENDING = "pending"
    EMAIL_SENT = "sent"
    EMAIL_FAILED = "failed"
    EMAIL_SKIPPED = "skipped"
    EMAIL_STATUSES = (
        (EMAIL_PENDING, "Pending"),
        (EMAIL_SENT, "Sent"),
        (EMAIL_FAILED, "Failed"),
        (EMAIL_SKIPPED, "Skipped"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_history",
    )
    company = models.ForeignKey(ClientCompany, on_delete=models.CASCADE, related_name="alert_history")
    watchlist_entry = models.ForeignKey(
        WatchlistEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_history",
    )
    alert_type = models.CharField(
        max_length=40,
        choices=AlertSetting.ALERT_TYPES + (
            (WatchlistAlertRule.VALUE_DIFFERENCE, "Value difference"),
            (WatchlistAlertRule.DATE_OCCURRENCE, "Date occurrence"),
        ),
    )
    symbol = models.CharField(max_length=50)
    message = models.TextField()
    triggered_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    email_status = models.CharField(max_length=20, choices=EMAIL_STATUSES, default=EMAIL_PENDING)
    email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "created_at"]), models.Index(fields=["symbol"])]

    def __str__(self):
        return f"{self.company} - {self.symbol} - {self.get_alert_type_display()}"

