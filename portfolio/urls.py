from django.urls import path
from . import views

urlpatterns = [
    path("",         views.overview,          name="portfolio-overview"),
    path("upload/",  views.upload_page,        name="portfolio-upload"),
    path("upload/csv/", views.upload_csv,      name="portfolio-upload-csv"),
    path("manual/",  views.add_manual,         name="portfolio-manual"),
    path("holdings/",views.holdings_partial,   name="portfolio-holdings"),
    path("simulate/",views.simulate_partial,   name="portfolio-simulate"),
    path("tax/",     views.tax_partial,        name="portfolio-tax"),
    path("discover/suggestions/", views.suggestions_partial, name="portfolio-suggestions"),
    path("discover/search/",      views.search_partial,      name="portfolio-search"),
    path("quick-add/",            views.quick_add,           name="portfolio-quick-add"),
    path("clear/portfolio/",      views.clear_portfolio,     name="portfolio-clear-portfolio"),
    path("clear/strategy/",       views.clear_strategy,      name="portfolio-clear-strategy"),
    path("ai-review/",            views.ai_review_partial,   name="portfolio-ai-review"),
]
