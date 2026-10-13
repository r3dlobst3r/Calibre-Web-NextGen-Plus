# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression pin for fork issue #799: the new UI's Help menu "Report Issue on
GitHub" link opened the blank-issue form (``/issues/new``) instead of the
template chooser, so reporters landed on an empty textarea instead of the
Bug report / Feature request forms defined in ``.github/ISSUE_TEMPLATE/``.

The fix points the link at ``/issues/new/choose`` (adopts @chloeroform's #800).
This pin keeps the chooser URL in place so a future refactor can't silently
revert to the bare ``/issues/new`` form.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPBAR_TSX = REPO_ROOT / "frontend" / "src" / "components" / "TopBar.tsx"

# The bare blank-issue URL (closing quote immediately after `/issues/new`) is
# the pre-fix target; the chooser URL is `/issues/new/choose`, so this exact
# literal must not appear once the fix is in.
_BLANK_ISSUE_URL = "'https://github.com/r3dlobst3r/Calibre-Web-NextGen-Plus/issues/new'"
_CHOOSER_URL = "https://github.com/r3dlobst3r/Calibre-Web-NextGen-Plus/issues/new/choose"


def test_help_menu_issue_link_uses_template_chooser():
    src = TOPBAR_TSX.read_text(encoding="utf-8")
    assert _CHOOSER_URL in src, (
        "The Help menu's 'Report Issue on GitHub' link must point at the "
        "template chooser (/issues/new/choose) so reporters get the Bug report "
        "form, not a blank issue (#799)."
    )
    assert _BLANK_ISSUE_URL not in src, (
        "The bare blank-issue URL (/issues/new with no /choose) must not be "
        "the Report-Issue target (#799) — it bypasses the issue templates."
    )
