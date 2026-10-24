"""Entry point for the W2 free-channel acquisition collector (dev-plan section 6).

Usage (all offline by default; run/resume are gated and currently ALWAYS
refuse because no independent review seal exists yet):

    python -B -X utf8 scripts/acquire_futures_free.py plan --policy <policy.json> \
        --inventory <inventory.json> --output <new_plan_dir>
    python -B -X utf8 scripts/acquire_futures_free.py verify-package --package <dir> --offline
    python -B -X utf8 scripts/acquire_futures_free.py run --plan <plan.json> \
        --authorization <user_confirmation.json> --review-seal <review-seal.json>
    python -B -X utf8 scripts/acquire_futures_free.py resume --plan <plan.json> \
        --authorization <same_valid_authorization.json> --review-seal <seal.json>

Expected exit codes: 0 ok; 2 gate refusal (before any adapter/engine is
constructed); 3 blocked (account/permission); 4 failure/integrity problems;
5 deferred budget (clean partial progress, resumable).
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from ak_trading.acquisition.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
