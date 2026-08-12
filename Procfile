web: gunicorn Papertrail.asgi:application -k uvicorn.workers.UvicornWorker --workers 3 --bind 0.0.0.0:8000

dramatiq_default: python manage.py rundramatiq --processes 1 --threads 10 -Q default
dramatiq_ocr: python manage.py rundramatiq --processes 1 --threads 2 -Q ocr-tasks
dramatiq_maintenance: python manage.py rundramatiq --processes 1 --threads 1 -Q maintenance

scheduler: python manage.py runperiodiq
