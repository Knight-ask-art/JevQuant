"""Read-only realised-cost report over recorded fills.

    python -m jev_trading.cost_report [--days 30] [--limit 500] [--json]
"""

from .services.cost_report import main

if __name__ == "__main__":
    main()
