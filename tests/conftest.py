"""Test-suite isolation from a personal buyer profile.

Many tests encode the repo owner's mandates and an SC buyer. A fork's
git-ignored profile.local.json must not change what those tests see, so the
suite runs with the profile disabled unless a test opts back in (set
PF_PROFILE to a fixture path and call buyer_profile.reset_cache()).
Set at import time: weekly.py reads the profile when it is first imported.
"""

import os

os.environ["PF_PROFILE"] = "none"
