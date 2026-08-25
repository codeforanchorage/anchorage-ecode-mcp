"""Invariants on the shipped deployment configuration.

Unlike the rest of the suite, these tests read the REAL files this fork
deploys (``config.yaml``, ``config-ecode.yaml``, ``terraform/aws/prod.tfvars``)
rather than fixtures. They exist because the values below are only wrong at
runtime, in production, under load -- there is no unit under test that would
otherwise notice.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config.yaml"
CONFIG_ECODE = REPO_ROOT / "config-ecode.yaml"
PROD_TFVARS = REPO_ROOT / "terraform" / "aws" / "prod.tfvars"

# API Gateway REST integrations time out at 29 seconds. This is a hard AWS
# service limit -- not a quota, not adjustable by support -- so every timeout
# underneath it must be strictly smaller. See terraform/aws/api_gateway.tf,
# which uses aws_api_gateway_rest_api.
API_GATEWAY_INTEGRATION_TIMEOUT = 29


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tfvar_int(name: str) -> int:
    """Read a bare integer variable out of prod.tfvars."""
    text = PROD_TFVARS.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, f"{name} not found in {PROD_TFVARS}"
    return int(match.group(1))


class TestTimeoutLadder:
    """API Gateway (29s) > Lambda > plugin HTTP, strictly decreasing.

    Inverting any pair produces a failure mode that unit tests cannot see:

    * Lambda above the gateway -- the client gets a 504 at 29s while the
      Lambda keeps burning a reserved-concurrency slot for the remainder.
    * Plugin above the Lambda -- a hung upstream never yields a readable
      "upstream timed out" tool error; the Lambda is killed mid-flight and
      the caller gets an opaque 502.
    """

    def test_lambda_timeout_below_api_gateway_ceiling(self):
        lambda_timeout = _load(CONFIG)["aws"]["lambda_timeout"]
        assert lambda_timeout < API_GATEWAY_INTEGRATION_TIMEOUT, (
            f"lambda_timeout={lambda_timeout}s is at or above API Gateway's "
            f"hard {API_GATEWAY_INTEGRATION_TIMEOUT}s integration timeout"
        )

    def test_plugin_timeout_below_lambda_timeout(self):
        config = _load(CONFIG)
        lambda_timeout = config["aws"]["lambda_timeout"]
        plugin_timeout = config["plugins"]["ecode"]["timeout"]
        assert plugin_timeout < lambda_timeout, (
            f"plugin timeout={plugin_timeout}s is at or above "
            f"lambda_timeout={lambda_timeout}s"
        )

    def test_plugin_timeout_leaves_headroom_to_return(self):
        """The plugin must finish AND render its response inside the Lambda."""
        config = _load(CONFIG)
        headroom = config["aws"]["lambda_timeout"] - config["plugins"]["ecode"]["timeout"]
        assert headroom >= 5, (
            f"only {headroom}s between the plugin timeout and the Lambda "
            "timeout; a slow upstream would leave no time to format and "
            "return the error"
        )


class TestTerraformAgreesWithConfig:
    """config.yaml wins for these, but drift still misleads whoever reads
    prod.tfvars -- see the locals block in terraform/aws/main.tf."""

    @pytest.mark.parametrize("key", ["lambda_timeout", "lambda_memory"])
    def test_tfvars_matches_config(self, key):
        assert _tfvar_int(key) == _load(CONFIG)["aws"][key], (
            f"{key} differs between config.yaml (authoritative) and "
            f"prod.tfvars; main.tf reads config.yaml first, so the tfvars "
            f"value is silently ignored"
        )


class TestConfigCopiesInSync:
    """config.yaml and config-ecode.yaml are byte-identical copies.

    deploy.sh packages config.yaml; config-ecode.yaml is the named copy kept
    for reference. Nothing enforces the relationship at deploy time, so a fix
    applied to only one of them ships half-applied.
    """

    def test_identical(self):
        assert (
            CONFIG.read_bytes() == CONFIG_ECODE.read_bytes()
        ), "config.yaml and config-ecode.yaml have drifted apart"
