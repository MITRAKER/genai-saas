import os

# The service refuses to start without a JWT secret (see app/config.py),
# so the tests bring their own. It must exist before `app` is imported.
os.environ.setdefault("JWT_SECRET", "test-only-" + "x" * 32)
