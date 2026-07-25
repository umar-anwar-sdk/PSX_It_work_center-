import json
from decimal import Decimal
from pathlib import Path

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



