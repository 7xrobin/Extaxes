from django.db import models

from agent import catalog


class UserProfile(models.Model):
    user_id = models.CharField(max_length=100, unique=True, default="demo")
    savings_total = models.FloatField(default=0)
    emergency_fund_floor = models.FloatField(default=0)
    monthly_investment_budget = models.FloatField(default=0)
    risk_profile = models.CharField(max_length=20, default="balanced")
    tax_bracket = models.FloatField(default=0.42)
    is_married = models.BooleanField(default=False)
    intake_complete = models.BooleanField(default=False)
    strategy_approved = models.BooleanField(default=False)
    approved_strategy_text = models.TextField(blank=True, default="")
    approved_strategy_data = models.JSONField(null=True, blank=True)
    strategy_approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def investable_surplus(self):
        return max(0, self.savings_total - self.emergency_fund_floor)

    def __str__(self):
        return self.user_id


class Goal(models.Model):
    profile = models.ForeignKey(
        UserProfile, related_name="goals", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=200)
    target_amount = models.FloatField(default=0)
    target_date = models.CharField(max_length=20, default="open")
    monthly_allocation = models.FloatField(default=0)
    priority = models.IntegerField(default=1)

    def __str__(self):
        return f"{self.name} ({self.profile.user_id})"


class Holding(models.Model):
    # Single source of truth: agent/catalog.py. Choices only — values are
    # unchanged, so this never triggers a migration.
    ASSET_TYPES = catalog.asset_type_choices()
    profile = models.ForeignKey(
        UserProfile, related_name="holdings", on_delete=models.CASCADE
    )
    ticker = models.CharField(max_length=20)
    isin = models.CharField(max_length=12, blank=True)
    asset_type = models.CharField(max_length=10, choices=ASSET_TYPES)
    plan_category = models.CharField(max_length=100, blank=True, default="")
    units = models.FloatField()
    avg_purchase_price = models.FloatField()
    purchase_date = models.DateField(null=True, blank=True)
    current_price = models.FloatField(default=0)
    current_value = models.FloatField(default=0)
    unrealised_gain = models.FloatField(default=0)
    unrealised_gain_pct = models.FloatField(default=0)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.ticker} ({self.profile.user_id})"


class ExitRule(models.Model):
    STOP_ACTIONS = [
        ("hold",   "Hold"),
        ("review", "Review"),
        ("sell",   "Sell"),
    ]
    holding = models.OneToOneField(
        Holding, related_name="exit_rule", on_delete=models.CASCADE
    )
    sell_at_gain_pct = models.FloatField(default=0.30)
    sell_portion_pct = models.FloatField(default=0.50)
    stop_loss_pct = models.FloatField(default=-0.15)
    stop_loss_action = models.CharField(
        max_length=10, choices=STOP_ACTIONS, default="review"
    )
    sell_before_year_end = models.BooleanField(default=False)
    note = models.TextField(blank=True)
