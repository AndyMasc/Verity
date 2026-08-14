import os

from . import base

globals().update({k: v for k, v in vars(base).items() if not k.startswith("_")})

if os.environ.get("DJANGO_ENV") == "production":
    try:
        from . import production

        for key, value in vars(production).items():
            if not key.startswith("_"):
                globals()[key] = value
    except ImportError:
        pass
else:
    from . import local

    for key, value in vars(local).items():
        if not key.startswith("_"):
            globals()[key] = value
