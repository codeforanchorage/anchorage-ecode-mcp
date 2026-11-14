"""The MCP server core is a dependency (opencontext-core), not a copy.

These check the seam between that package and this repo: the core must be
importable, must find this repo's plugins, and must be pinned to the same
version everywhere it is declared.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"opencontext-core @ git\+https://\S+?@(v[\d.]+)")


def test_core_is_not_vendored():
    """A stray core/ or server/ directory here would shadow the package
    and silently pin this deployment to whatever was copied in."""
    assert not (REPO_ROOT / "core").exists()
    assert not (REPO_ROOT / "server").exists()


def test_core_and_lambda_handler_import():
    from server.adapters.aws_lambda import lambda_handler

    assert callable(lambda_handler)


def test_core_discovers_this_repos_enabled_plugin(monkeypatch):
    from core.plugin_manager import PluginManager
    from core.validators import get_enabled_plugin_config

    monkeypatch.chdir(REPO_ROOT)
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text("utf-8"))
    enabled, _ = get_enabled_plugin_config(config)
    discovered = dict(PluginManager(config).discover_plugins())

    assert enabled in discovered
    assert discovered[enabled].parent == REPO_ROOT / "plugins"


def test_installed_core_matches_the_pin():
    """A stale environment would otherwise test one core and ship
    another: the pin is what deploy.sh installs."""
    import importlib.metadata

    pinned = PIN.findall((REPO_ROOT / "requirements.txt").read_text("utf-8"))
    assert ["v" + importlib.metadata.version("opencontext-core")] == pinned


def test_core_pin_is_the_same_everywhere():
    pins = {
        name: PIN.findall((REPO_ROOT / name).read_text("utf-8"))
        for name in ("requirements.txt", "pyproject.toml")
    }
    assert all(len(found) == 1 for found in pins.values()), pins
    assert len({found[0] for found in pins.values()}) == 1, pins
