"""SnapIN backend package.

Note: `server.py` imports its siblings (`auth`, `detector`) by bare name so
that it works under `python backend/server.py`, `uvicorn server:app` from
inside `backend/`, and gunicorn `--chdir backend`. Importing this package is
only needed for `python -m backend.<module>`.
"""