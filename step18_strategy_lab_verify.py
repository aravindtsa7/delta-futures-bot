"""Step 18 verification using isolated synthetic fixtures, never user research/journal files."""
import testenv  # noqa: F401
import sys
from pathlib import Path


def main() -> int:
    try:
        import pytest
    except ImportError:
        print("Install test dependencies: python -m pip install -r requirements-dev.txt")
        return 1
    return pytest.main(["-q", str(Path(__file__).with_name("test_review_repairs.py")),
                        "-k", "step18_isolated_http_roundtrip or save_authority_full_ledger_and_deploy"])


if __name__ == "__main__":
    sys.exit(main())
