"""Allow `python -m neo.services.benchmark`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
