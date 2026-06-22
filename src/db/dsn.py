"""DB_URL → filesystem path helper.

Supported forms:
  - ``sqlite:///path/to/file.db`` → ``/path/to/file.db``  (file-backed)
  - ``sqlite:///:memory:``        → None                   (in-memory)
  - empty / unset                 → None                   (in-memory)

Postgres DSNs are explicitly out of scope for this implementation; calling
``db_path_from_url`` with a Postgres DSN raises ``NotImplementedError`` with
a clear message so operators know what happened.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def db_path_from_url(db_url: str | None) -> str | None:
    """Parse *db_url* and return the SQLite filesystem path, or ``None`` for in-memory.

    Args:
        db_url: The ``DB_URL`` env-var value (may be ``None`` or empty).

    Returns:
        A non-empty filesystem path string when the URL names a file, or
        ``None`` when the URL is absent, empty, or names ``:memory:``.

    Raises:
        NotImplementedError: When *db_url* is a Postgres (``postgresql://`` /
            ``postgres://``) DSN — not implemented; operator must use SQLite or
            omit DB_URL.
    """
    if not db_url:
        return None

    url = db_url.strip()

    if url.startswith(("postgresql://", "postgres://")):
        raise NotImplementedError(
            f"Postgres DSNs are not yet supported (got {url!r}). "
            "Use a sqlite:///... URL or leave DB_URL unset for in-memory mode."
        )

    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        if path == ":memory:":
            return None
        if not path:
            _log.warning(
                "DB_URL %r has an empty path after 'sqlite:///'; using in-memory store.",
                url,
            )
            return None
        return path

    if url in ("sqlite:///:memory:", "sqlite://:memory:"):
        return None

    # Unknown scheme or bare path — warn and fall back to in-memory.
    _log.warning(
        "DB_URL %r is not a recognised SQLite URL; using in-memory store. "
        "Expected format: sqlite:///path/to/file.db",
        url,
    )
    return None
