import threading
from uuid import UUID

import pytest

from config.run_id import bind_run_id, current_run_id, new_run_id


def test_outside_chain_is_none() -> None:
    assert current_run_id() is None


def test_new_run_ids_differ() -> None:
    assert new_run_id() != new_run_id()


def test_bind_sets_and_restores() -> None:
    run_id = new_run_id()
    with bind_run_id(run_id) as bound:
        assert bound == run_id
        assert current_run_id() == run_id
    assert current_run_id() is None


def test_bind_accepts_string() -> None:
    run_id = new_run_id()
    with bind_run_id(str(run_id)) as bound:
        assert isinstance(bound, UUID)
        assert current_run_id() == run_id


def test_bind_rejects_garbage() -> None:
    with pytest.raises(ValueError), bind_run_id("not-a-uuid"):
        pass


def test_nested_restores_outer() -> None:
    outer, inner = new_run_id(), new_run_id()
    with bind_run_id(outer):
        with bind_run_id(inner):
            assert current_run_id() == inner
        assert current_run_id() == outer


def test_restored_after_exception() -> None:
    with pytest.raises(RuntimeError), bind_run_id(new_run_id()):
        raise RuntimeError
    assert current_run_id() is None


def test_other_thread_does_not_see_run_id() -> None:
    seen: list[UUID | None] = []
    with bind_run_id(new_run_id()):
        thread = threading.Thread(target=lambda: seen.append(current_run_id()))
        thread.start()
        thread.join()
    assert seen == [None]
