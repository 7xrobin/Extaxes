from django.urls import path
from . import views

urlpatterns = [
    path("",          views.digest_page,     name="digest"),
    path("generate/", views.generate_digest, name="digest-generate"),
]
