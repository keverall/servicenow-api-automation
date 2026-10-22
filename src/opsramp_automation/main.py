"""Command line entry point for ``opsramp-automation``.

The interactive CLI — the typer application object, all of its commands and
the shared global options — lives in :mod:`opsramp_automation.cli`.  This module
is a thin, importable entry point so that the package can be launched with
``python -m opsramp_automation`` and so the installed ``opsramp-gw`` console
script resolves to a conventional ``main`` callable rather than a typer
application instance.
"""

from __future__ import annotations

import sys

from .cli import app

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the ``opsramp-gw`` command line interface.

    Typer's ``Typer.__call__`` delegates to Click, which raises
    :class:`SystemExit` when argument parsing finishes.  We intercept it and
    return the code instead, which makes :func:`main` safe to call from other
    Python code and from ``__main__``.
    """
    try:
        app(args=argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
