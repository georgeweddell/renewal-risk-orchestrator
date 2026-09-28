from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rro.db import Store
from rro.governance.gateway import ToolGateway
from rro.governance.policy import Policy
from rro.seeding import load_seed, seed_mock_systems
from rro.settings import PROJECT_ROOT, Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """An isolated project home with the real config and seed, freshly seeded, and no .env."""
    for folder in ("config", "seed"):
        shutil.copytree(PROJECT_ROOT / folder, tmp_path / folder)
    s = Settings(_env_file=None, rro_home=tmp_path, rro_mode="mock")
    seed_mock_systems(load_seed(s.seed_file), s.mock_db)
    return s


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store.open(settings.rro_db)


def make_gateway(settings: Settings, store: Store) -> ToolGateway:
    """Use as `async with make_gateway(...)` inside the test body: the MCP client's
    task groups must be entered and exited in the same task."""
    return ToolGateway(settings, Policy.load(settings.config_dir / "policy.yaml"), store)
