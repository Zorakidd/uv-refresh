"""uv-refresh: pyproject.toml neu aufbauen und Dependencies frisch aufloesen."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uv-refresh")
except PackageNotFoundError:  # z. B. aus einem Source-Checkout ohne Installation
    __version__ = "0.0.0+unknown"
