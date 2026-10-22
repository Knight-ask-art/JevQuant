"""Read-only exit diagnostics over recorded counterfactual outcomes.

    python -m jev_trading.exit_diagnostics [--limit 500] [--json]
"""

from .services.exit_diagnostics import main

if __name__ == "__main__":
    main()
