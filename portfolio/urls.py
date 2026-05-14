from django.urls import path
from . import views

urlpatterns = [
    path("",         views.overview,          name="portfolio-overview"),
    path("upload/",  views.upload_page,        name="portfolio-upload"),
    path("upload/csv/", views.upload_csv,      name="portfolio-upload-csv"),
    path("manual/",  views.add_manual,         name="portfolio-manual"),
    path("holdings/",views.holdings_partial,   name="portfolio-holdings"),
    path("tax/",     views.tax_partial,        name="portfolio-tax"),
    path("discover/suggestions/", views.suggestions_partial, name="portfolio-suggestions"),
    path("discover/search/",      views.search_partial,      name="portfolio-search"),
]
