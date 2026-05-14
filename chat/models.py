from django.db import models
from django.contrib.auth.models import User


class ChatSession(models.Model):
    user      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_sessions')
    thread_id = models.CharField(max_length=100, unique=True)
    title     = models.CharField(max_length=200, default='New Chat')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} — {self.title}"
