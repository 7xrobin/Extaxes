from django.contrib import admin
from .models import UserProfile, Goal, Holding, ExitRule

admin.site.register(UserProfile)
admin.site.register(Goal)
admin.site.register(Holding)
admin.site.register(ExitRule)
