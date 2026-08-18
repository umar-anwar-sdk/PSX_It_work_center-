from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction

from .models import AlertSetting, ClientCompany, CompanySettings, MODULE_CHOICES, ModulePermission


User = get_user_model()


class ClientCompanyCreateForm(UserCreationForm):
    company_name = forms.CharField(max_length=255)
    contact_name = forms.CharField(max_length=255, required=False)
    phone = forms.CharField(max_length=50, required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name")

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            company = ClientCompany.objects.create(
                user=user,
                company_name=self.cleaned_data["company_name"],
                contact_name=self.cleaned_data.get("contact_name", ""),
                phone=self.cleaned_data.get("phone", ""),
            )
            CompanySettings.objects.create(company=company)
            for module, _label in MODULE_CHOICES:
                ModulePermission.objects.create(
                    company=company,
                    module=module,
                    can_view=module in {"dashboard", "settings_profile"},
                )
            for alert_type, _label in AlertSetting.ALERT_TYPES:
                AlertSetting.objects.create(company=company, alert_type=alert_type)
        return user


class ClientCompanyUpdateForm(forms.Form):
    company_name = forms.CharField(max_length=255)
    contact_name = forms.CharField(max_length=255, required=False)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50, required=False)
    is_active = forms.BooleanField(required=False)


class ClientProfileUpdateForm(forms.Form):
    company_name = forms.CharField(max_length=255)
    contact_name = forms.CharField(max_length=255, required=False)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50, required=False)


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = ("email_notifications", "watchlist_alerts", "default_market", "timezone")
