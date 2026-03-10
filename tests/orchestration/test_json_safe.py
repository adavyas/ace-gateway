from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ace_net_manager.utils.json_safe import to_jsonable


pytestmark = pytest.mark.unit


def test_to_jsonable_normalizes_uuid_datetime_and_sets():
    uid = uuid4()
    value = {
        "uid": uid,
        "seen_at": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        "tags": {"a", "b"},
        "nested": (1, 2, 3),
    }

    out = to_jsonable(value)

    assert out["uid"] == str(uid)
    assert out["seen_at"] == "2026-01-01T00:00:00+00:00"
    assert sorted(out["tags"]) == ["a", "b"]
    assert out["nested"] == [1, 2, 3]
