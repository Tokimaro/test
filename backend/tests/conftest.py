import pytest

from app.config import RunMode, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, mode=RunMode.PAPER, log_json=False)
