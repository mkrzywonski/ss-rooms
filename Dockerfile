FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
RUN groupadd -r lodge && useradd -r -g lodge lodge && mkdir -p /app/media /app/staticfiles && chown -R lodge:lodge /app
COPY --chown=lodge:lodge . .
USER lodge
RUN DJANGO_SECRET_KEY=build-only-not-used-at-runtime python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--access-logformat", "%(m)s %(s)s %(L)s", "--error-logfile", "-"]
