from django.urls import path
from . import views

urlpatterns = [
    path("",         views.chat_page,     name="chat"),
    path("message/", views.send_message,  name="chat-message"),
    path("reset/",   views.reset_session, name="chat-reset"),
]
