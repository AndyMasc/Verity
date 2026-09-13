web: gunicorn Verity.asgi:application -k uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:8000
worker_general: python manage.py rundramatiq --processes 1 --threads 4 -Q default maintenance
worker_ocr: python manage.py rundramatiq --processes 1 --threads 2 -Q ocr-tasks
scheduler: python manage.py runperiodiq
