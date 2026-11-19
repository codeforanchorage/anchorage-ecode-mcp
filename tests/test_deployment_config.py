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

    def test_call_budget_fits_inside_the_lambda(self):
        """All upstream requests in ONE tool call (retries, a section-number
        lookup then the fetch) share TOOL_CALL_BUDGET_S. It must end early
        enough to format and return before the Lambda is killed."""
        from plugins.ecode.plugin import TOOL_CALL_BUDGET_S

        lambda_timeout = _load(CONFIG)["aws"]["lambda_timeout"]
        assert lambda_timeout - TOOL_CALL_BUDGET_S >= 3, (
            f"TOOL_CALL_BUDGET_S={TOOL_CALL_BUDGET_S}s leaves under 3s "
            f"before lambda_timeout={lambda_timeout}s"
        )

    def test_call_budget_covers_one_full_request(self):
        """Below the plugin timeout, every first request would be cut short
        of the timeout the config promises."""
        from plugins.ecode.plugin import TOOL_CALL_BUDGET_S

        plugin_timeout = _load(CONFIG)["plugins"]["ecode"]["timeout"]
        assert TOOL_CALL_BUDGET_S > plugin_timeout


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


API_GATEWAY_TF = REPO_ROOT / "terraform" / "aws" / "api_gateway.tf"
MAIN_TF = REPO_ROOT / "terraform" / "aws" / "main.tf"
SHARED_CACHE_TF = REPO_ROOT / "terraform" / "aws" / "shared_cache.tf"


class TestCapacity:
    """Throttle and concurrency settings that only bite under real load."""

    def test_stage_throttle_reads_the_tfvars(self):
        """The stage-wide method settings must use the tfvars, not literals.

        They are the ONLY throttle on the unkeyed public /mcp route (the
        usage plan applies to API-key requests). They were once hard-coded
        to 10/5, so raising api_*_limit in prod.tfvars silently changed
        nothing for real users.
        """
        text = API_GATEWAY_TF.read_text(encoding="utf-8")
        block = re.search(
            r'resource "aws_api_gateway_method_settings" "mcp_post" \{'
            r".*?\n\}",
            text,
            re.DOTALL,
        )
        assert block, "aws_api_gateway_method_settings.mcp_post not found"
        body = block.group(0)
        assert re.search(
            r"throttling_burst_limit\s*=\s*var\.api_burst_limit", body
        )
        assert re.search(
            r"throttling_rate_limit\s*=\s*var\.api_rate_limit", body
        )

    def test_burst_at_least_rate(self):
        assert _tfvar_int("api_burst_limit") >= _tfvar_int("api_rate_limit")

    def test_reserved_concurrency_covers_burst(self):
        """Every request the gateway admits in a burst should find a Lambda
        slot; otherwise the overflow surfaces as Lambda throttles (502s)
        rather than clean gateway 429s."""
        assert _tfvar_int("lambda_reserved_concurrency") >= _tfvar_int(
            "api_burst_limit"
        )


class TestAbuseGuards:
    """Settings that exist to limit and surface abuse of the public route."""

    def test_request_body_is_capped(self):
        """Without server_max_body_bytes the only limit on a request body is
        Lambda's 6 MB payload ceiling. Real requests are a few KB."""
        limit = _load(CONFIG).get("server_max_body_bytes")
        assert isinstance(limit, int) and 0 < limit <= 1024 * 1024

    def test_alarms_notify_a_topic(self):
        """With alarm_sns_topic_arn unset the alarms are created with no
        actions: they change state and nobody is told."""
        text = PROD_TFVARS.read_text(encoding="utf-8")
        assert re.search(
            r'^alarm_sns_topic_arn\s*=\s*"arn:aws:sns:[^"]+"\s*$',
            text,
            re.MULTILINE,
        )


class TestSinglePublicAddress:
    """With a custom domain, the default execute-api URL must stay off."""

    def test_default_endpoint_is_disabled_and_redeployed(self):
        text = API_GATEWAY_TF.read_text(encoding="utf-8")
        assert re.search(
            r'disable_execute_api_endpoint\s*=\s*var\.custom_domain != ""',
            text,
        )
        # API Gateway applies the flag only on a new deployment, so the
        # deployment's trigger hash has to include it.
        deployment = re.search(
            r'resource "aws_api_gateway_deployment" "mcp_deployment" \{'
            r".*?\n\}",
            text,
            re.DOTALL,
        )
        assert deployment
        assert (
            "aws_api_gateway_rest_api.mcp_api.disable_execute_api_endpoint"
            in deployment.group(0)
        )


class TestSharedCacheBucket:
    """terraform/aws/shared_cache.tf and the plugin must agree, and the
    Lambda must hold no more than object read/write on the bucket."""

    def test_lambda_gets_the_variable_the_plugin_reads(self):
        from plugins.ecode.plugin import SHARED_CACHE_ENV

        text = MAIN_TF.read_text(encoding="utf-8")
        assert re.search(
            rf"{SHARED_CACHE_ENV}\s*=\s*aws_s3_bucket\.shared_cache\.bucket",
            text,
        )

    def test_role_can_only_get_and_put_objects(self):
        text = SHARED_CACHE_TF.read_text(encoding="utf-8")
        actions = re.findall(r'"(s3:[A-Za-z*]+)"', text)
        assert sorted(actions) == ["s3:GetObject", "s3:PutObject"]
        assert '"${aws_s3_bucket.shared_cache.arn}/*"' in text

    def test_bucket_is_not_public(self):
        text = SHARED_CACHE_TF.read_text(encoding="utf-8")
        for setting in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        ):
            assert re.search(rf"{setting}\s*=\s*true", text), setting

    def test_plugin_ignores_objects_before_the_bucket_expires_them(self):
        from plugins.ecode.plugin import SHARED_CACHE_MAX_AGE_S

        text = SHARED_CACHE_TF.read_text(encoding="utf-8")
        days = int(re.search(r"expiration\s*{\s*days\s*=\s*(\d+)", text).group(1))
        assert SHARED_CACHE_MAX_AGE_S <= days * 86400
