import pytest

from tests._isolation import reset_process_caches


@pytest.fixture(autouse=True)
def _reset_process_state():
    reset_process_caches()
