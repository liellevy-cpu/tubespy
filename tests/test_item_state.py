"""Per-video library state: Used / Not relevant, and the seeded Used item."""
import pytest

from add_brand import Invalid, Store

VID = "GmrgKvRelpk"


def test_set_get_and_clear_item_state(tmp_path):
    store = Store(tmp_path / "q.sqlite")
    assert store.item_states() == {}
    store.set_item_state(VID, "used")
    assert store.item_states() == {VID: "used"}
    store.set_item_state(VID, "not_relevant")
    assert store.item_states() == {VID: "not_relevant"}
    store.set_item_state(VID, "new")  # clears
    assert store.item_states() == {}


def test_item_state_validates_inputs(tmp_path):
    store = Store(tmp_path / "q.sqlite")
    with pytest.raises(Invalid):
        store.set_item_state("short", "used")
    with pytest.raises(Invalid):
        store.set_item_state(VID, "bogus")


def test_seed_used_is_idempotent_and_non_destructive(tmp_path):
    store = Store(tmp_path / "q.sqlite")
    store.seed_used([VID])
    assert store.item_states() == {VID: "used"}
    # a later manual change is not overwritten by re-seeding
    store.set_item_state(VID, "not_relevant")
    store.seed_used([VID])
    assert store.item_states() == {VID: "not_relevant"}
