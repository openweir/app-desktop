"""Allow ``python -m openweir``."""

from openweir.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
