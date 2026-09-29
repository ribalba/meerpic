"""Test-wide setup.

``MEERPIC_CONFIG=""`` before anything imports the config: the loader would
otherwise read the developer's own ``meerpic.toml``, and a test that passes
because of what is on one machine is not a test. ``MEERPIC_EMBED_FAKE=1`` for
the same reason: the real search model is a 1.5 GB download, and the tests are
about this program's use of vectors, not about the model's opinions.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MEERPIC_CONFIG", "")
os.environ.setdefault("MEERPIC_EMBED_FAKE", "1")
# The NSFW classifier likewise: an 88 MB download, replaced by a colour rule.
os.environ.setdefault("MEERPIC_NSFW_FAKE", "1")
# The face models too: 190 MB, replaced by "a coloured patch is a face".
os.environ.setdefault("MEERPIC_FACES_FAKE", "1")
# And the text models: 23 MB, replaced by "a red picture says RED".
os.environ.setdefault("MEERPIC_OCR_FAKE", "1")
os.environ.setdefault("TZ", "Europe/Berlin")
# A library and a cache nobody keeps anything in. Tests that need files write
# them under these; the fixtures in tests/fixtures.py do most of it.
_scratch = Path(tempfile.mkdtemp(prefix="meerpic-test-"))
os.environ.setdefault("LIBRARY_ROOTS", '{"icloud": "%s"}' % (_scratch / "library"))
os.environ.setdefault("CACHE_DIR", str(_scratch / "cache"))
(_scratch / "library").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The database tests need a real Postgres with pgvector: the queries they cover
# are Postgres' (vector distance, JSONB, trigram), so SQLite would prove
# nothing. They run against MEERPIC_TEST_DB and skip without it, so that
# `pytest` on a laptop with no database still runs everything else, and so that
# a test run can never be pointed at the database your photos are indexed in.
_test_db = os.environ.get("MEERPIC_TEST_DB")
if _test_db:
    os.environ["DATABASE_URL"] = _test_db


import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
