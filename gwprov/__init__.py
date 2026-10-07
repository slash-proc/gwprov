"""Python APIs for composing and provisioning Game & Watch firmware media."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gwprov")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
