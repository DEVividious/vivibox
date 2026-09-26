"""Which build this is: the version git gave the package at build time (a tag, or the commit
past it), for a bug report to start with."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed


def current() -> str:
    try:
        return installed("vivibox")
    except PackageNotFoundError:  # run from a checkout that was never installed
        return "0+unknown"
