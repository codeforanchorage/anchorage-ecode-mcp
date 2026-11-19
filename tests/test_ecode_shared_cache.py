"""The shared (S3) cache for the Ordinance Disposition Table (1.14.0).

eCode360 takes about 9 s to return that table. The in-memory cache belongs
to one Lambda container, so each new container made a caller wait; the
bucket lets every container read one stored copy. It is an optimisation,
never a dependency: every failure here must end in the table being
fetched from eCode360 and the tool call succeeding.
"""

import io
import json
import threading
import time

import pytest

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.plugin import EcodePlugin
from tests.test_ecode_find_ordinance import CCT_GUID, CCT_NODE, _find, _plugin

PATH = f"/customer/AN6998/code/content/{CCT_GUID}"
KEY = f"v1/customer/AN6998/code/content/{CCT_GUID}.json"
CODE_DATE = "2026-11-10"


class FakeS3:
    """get_object / put_object over a dict, as boto3's client offers them."""

    def __init__(self):
        self.objects = {}
        self.gets = []
        self.puts = []
        self.get_error = None
        self.put_error = None
        self.block = None

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        if self.block is not None:
            self.block.wait(5)
        if self.get_error:
            raise self.get_error
        if Key not in self.objects:
            raise type("NoSuchKey", (Exception,), {})("no such key")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts.append((Bucket, Key, ContentType))
        if self.put_error:
            raise self.put_error
        self.objects[Key] = Body


def _stored(**changes):
    body = {
        "version": 1,
        "path": PATH,
        "code_last_updated": CODE_DATE,
        "fetched_at": time.time() - 60,
        "data": CCT_NODE,
    }
    body.update(changes)
    return json.dumps(body).encode()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()

    async def dated(self):
        return CODE_DATE

    monkeypatch.setattr(EcodePlugin, "_code_last_updated", dated)
    monkeypatch.setattr(
        EcodePlugin, "_shared_cache_client", staticmethod(lambda: (fake, "bkt"))
    )
    return fake


def _table_fetches(plugin):
    return sum(1 for c in plugin.calls if c.endswith(CCT_GUID))


class TestSharedCache:
    @pytest.mark.asyncio
    async def test_first_container_fetches_and_stores(self, s3):
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1
        assert s3.gets == [("bkt", KEY)]
        assert s3.puts == [("bkt", KEY, "application/json")]
        stored = json.loads(s3.objects[KEY])
        assert stored["code_last_updated"] == CODE_DATE
        assert stored["path"] == PATH
        assert stored["data"] == CCT_NODE
        assert abs(stored["fetched_at"] - time.time()) < 60

    @pytest.mark.asyncio
    async def test_next_container_reads_it_instead_of_ecode360(self, s3):
        await _find("2025-115", _plugin())
        ecode_mod._STATIC_CACHE.clear()  # a different, cold container
        ecode_mod._RECENT_CACHE.clear()

        p = _plugin()
        sc, _ = await _find("2016-53", p)

        assert sc["found"] is True
        assert sc["matches"][0]["entry_count"] == 2
        assert _table_fetches(p) == 0
        assert len(s3.puts) == 1  # nothing rewritten on a hit

    @pytest.mark.asyncio
    async def test_warm_container_asks_neither(self, s3):
        p = _plugin()
        await _find("2025-115", p)
        await _find("2016-53", p)

        assert _table_fetches(p) == 1
        assert len(s3.gets) == 1


class TestStaleOrUnusableObjects:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"code_last_updated": "2026-10-01"},  # the code was republished
            {"fetched_at": time.time() - 8 * 24 * 3600},  # too old
            {"fetched_at": time.time() + 3600},  # a clock that cannot be right
            {"version": 2},
            {"path": "/customer/AN6998/code/content/1"},
            {"data": "not a node"},
            {"data": {"id": CCT_GUID}},  # no content
        ],
    )
    async def test_ignored_refetched_and_replaced(self, s3, changes):
        s3.objects[KEY] = _stored(**changes)
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1
        assert json.loads(s3.objects[KEY])["data"] == CCT_NODE
        assert json.loads(s3.objects[KEY])["code_last_updated"] == CODE_DATE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b"{}"])
    async def test_corrupt_object(self, s3, body):
        s3.objects[KEY] = body
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1

    @pytest.mark.asyncio
    async def test_oversized_object_is_not_parsed(self, s3, monkeypatch):
        monkeypatch.setattr(ecode_mod, "SHARED_CACHE_MAX_BYTES", 100)
        s3.objects[KEY] = _stored()
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1
        assert s3.puts == []  # and too large to store, said in the log


class TestTheBucketIsNeverADependency:
    @pytest.mark.asyncio
    async def test_read_failure(self, s3):
        s3.get_error = RuntimeError("AccessDenied")
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1

    @pytest.mark.asyncio
    async def test_write_failure(self, s3):
        s3.put_error = RuntimeError("AccessDenied")
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert KEY not in s3.objects

    @pytest.mark.asyncio
    async def test_slow_bucket_is_abandoned(self, s3, monkeypatch):
        monkeypatch.setattr(ecode_mod, "SHARED_CACHE_TIMEOUT_S", 0.1)
        s3.objects[KEY] = _stored()
        s3.block = threading.Event()
        p = _plugin()
        started = time.monotonic()
        try:
            sc, _ = await _find("2025-115", p)
        finally:
            s3.block.set()

        assert time.monotonic() - started < 3
        assert sc["found"] is True
        assert _table_fetches(p) == 1

    @pytest.mark.asyncio
    async def test_unknown_code_date_leaves_the_bucket_alone(
        self, s3, monkeypatch
    ):
        """Nothing to check a stored copy against, so neither read nor
        written."""

        async def unknown(self):
            return None

        monkeypatch.setattr(EcodePlugin, "_code_last_updated", unknown)
        s3.objects[KEY] = _stored()
        p = _plugin()
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert _table_fetches(p) == 1
        assert s3.gets == [] and s3.puts == []

    @pytest.mark.asyncio
    async def test_other_tools_do_not_touch_the_bucket(self, s3):
        p = _plugin()
        result = await p.execute_tool("recent_ordinances", {})

        assert result.success
        assert s3.gets == [] and s3.puts == []


class TestClient:
    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv(ecode_mod.SHARED_CACHE_ENV, raising=False)
        assert EcodePlugin._shared_cache_client() is None

    def test_boto3_missing_disables_it_once(self, monkeypatch):
        import sys

        monkeypatch.setenv(ecode_mod.SHARED_CACHE_ENV, "no-boto-bucket")
        monkeypatch.setitem(sys.modules, "boto3", None)  # import fails
        monkeypatch.setattr(ecode_mod, "_SHARED_CACHE_CLIENT", {})

        assert EcodePlugin._shared_cache_client() is None
        assert ecode_mod._SHARED_CACHE_CLIENT == {"no-boto-bucket": False}
        assert EcodePlugin._shared_cache_client() is None

    def test_key_is_versioned_and_per_library(self):
        assert EcodePlugin._shared_cache_key(PATH) == KEY
