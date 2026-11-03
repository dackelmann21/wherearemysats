"""Single source of truth for the application build version.

Used for frontend static-asset cache busting (every ``/static/`` URL in the
served index.html is appended with ``?v=<APP_VERSION>``) and the in-page brand
label.  Bump this value on every user-visible release.
"""

APP_VERSION = "0.19.7"
