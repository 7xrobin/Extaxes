from django.urls import path
from . import views

urlpatterns = [
    path("",                          views.chat_page,      name="chat"),
    path("message/",                  views.send_message,   name="chat-message"),
    path("new/",                      views.new_chat,       name="chat-new"),
    path("switch/<int:session_id>/",  views.switch_session, name="chat-switch"),
    path("delete/<int:session_id>/",  views.delete_session, name="chat-delete"),
]
