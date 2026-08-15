import os

from . import base

globals().update({k: v for k, v in vars(base).items() if not k.startswith("_")})

# Strip trailing whitespace and inline comments (e.g. "production # note") so a
# stray comment in .env can't silently switch the settings mode.
django_env = os.environ.get("DJANGO_ENV", "").partition("#")[0].strip()

if django_env == "production":
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
