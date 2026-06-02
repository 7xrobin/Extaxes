server:
    uv run python manage.py runserver

migrate:
    uv run python manage.py migrate

static:
    uv run python manage.py collectstatic --noinput

test:
    uv run python manage.py test agent portfolio chat digest rag --verbosity=2
