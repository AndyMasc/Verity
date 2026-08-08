web: gunicorn Papertrail.asgi:application -k uvicorn_worker.UvicornWorker --workers 8 --bind 127.0.0.1:8000
worker: python manage.py rundramatiq
scheduler: python manage.py runperiodiq