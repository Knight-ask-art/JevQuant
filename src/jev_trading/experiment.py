"""Offline A/B/C experiment over the deployed data.

    python -m jev_trading.experiment --days 3 [--json]
"""

from .services.experiment import main

if __name__ == "__main__":
    main()
