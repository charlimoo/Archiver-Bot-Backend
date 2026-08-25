# Archiver Backend

Build the production image with `docker build -t archiver-backend .`. It runs Gunicorn as a
non-root user. Apply migrations before starting it, or use the workspace Compose configuration,
which applies migrations automatically.

```bash
cp .env.example .env
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Run the worker in another terminal:

```bash
uv run celery -A config worker --loglevel=INFO
```

Run checks with `uv run ruff check .` and `uv run pytest`.
