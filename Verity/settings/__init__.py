import os

from django.core.exceptions import ImproperlyConfigured

from . import base

globals().update({k: v for k, v in vars(base).items() if not k.startswith("_")})

# Strip trailing whitespace and inline comments (e.g. "production # note") so a
# stray comment in .env can't silently switch the settings mode.
django_env = os.environ.get("DJANGO_ENV", "").partition("#")[0].strip()

if django_env == "production":
    # No ImportError swallowing: a broken production.py must crash startup, not
    # silently boot with zero production overrides.
    from . import production

    for key, value in vars(production).items():
        if not key.startswith("_"):
            globals()[key] = value
elif django_env == "ci":
    from . import ci

    for key, value in vars(ci).items():
        if not key.startswith("_"):
            globals()[key] = value
elif django_env == "local":
    from . import local

    for key, value in vars(local).items():
        if not key.startswith("_"):
            globals()[key] = value
else:
    # Refuse to guess: defaulting an internet-reachable deploy to local
    # settings means ALLOWED_HOSTS=["*"], CORS wide open, and cookies without
    # Secure — a one-missing-env-var downgrade of the entire security posture.
    raise ImproperlyConfigured(
        "DJANGO_ENV must be set explicitly to one of: 'local', 'ci', 'production'. "
        f"Got {django_env!r}. See .env.example."
    )
