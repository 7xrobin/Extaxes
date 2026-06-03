from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

urlpatterns = [
    path("", lambda request: redirect("/chat/")),
    path("accounts/", include("accounts.urls")),
    path("chat/", include("chat.urls")),
    path("portfolio/", include("portfolio.urls")),
    path("digest/", include("digest.urls")),
    path("admin/", admin.site.urls),
]
