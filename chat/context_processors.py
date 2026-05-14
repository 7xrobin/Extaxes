def chat_sessions(request):
    if not request.user.is_authenticated:
        return {}
    sessions = list(request.user.chat_sessions.all()[:15])
    active_id = request.session.get('active_chat_session_id')
    return {'chat_sessions': sessions, 'active_chat_session_id': active_id}
