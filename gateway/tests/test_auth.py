import time

import jwt as pyjwt
import pytest

from lens_gateway.auth import AuthStore


@pytest.fixture()
def store(tmp_path):
    return AuthStore(tmp_path, b"test-secret-32bytes-test-secret!")


class TestPairing:
    def test_pair_flow(self, store):
        code = store.new_pair_code()
        assert len(code) == 6
        result = store.pair(code, "测试手机")
        assert result is not None
        dev, refresh = result
        assert dev.device_id.startswith("dev_")
        assert len(refresh) > 30

    def test_code_single_use(self, store):
        code = store.new_pair_code()
        assert store.pair(code, "a") is not None
        assert store.pair(code, "b") is None

    def test_bad_code(self, store):
        assert store.pair("000000", "x") is None


class TestTokens:
    def test_access_roundtrip(self, store):
        dev, _ = store.pair(store.new_pair_code(), "p")
        token, exp = store.issue_access(dev.device_id)
        assert exp > time.time()
        assert store.verify_access(token) == dev.device_id

    def test_refresh_rotates_access(self, store):
        dev, refresh = store.pair(store.new_pair_code(), "p")
        got = store.refresh(refresh)
        assert got is not None and got[0] == dev.device_id

    def test_bad_refresh(self, store):
        assert store.refresh("nonsense") is None

    def test_revoke_blocks_both(self, store):
        dev, refresh = store.pair(store.new_pair_code(), "p")
        token, _ = store.issue_access(dev.device_id)
        store.revoke(dev.device_id)
        assert store.verify_access(token) is None
        assert store.refresh(refresh) is None

    def test_expired_token_raises(self, store, tmp_path):
        dev, _ = store.pair(store.new_pair_code(), "p")
        expired = pyjwt.encode({"sub": dev.device_id, "exp": int(time.time()) - 10},
                               b"test-secret-32bytes-test-secret!", algorithm="HS256")
        with pytest.raises(pyjwt.ExpiredSignatureError):
            store.verify_access(expired)

    def test_persistence(self, store, tmp_path):
        dev, refresh = store.pair(store.new_pair_code(), "p")
        store2 = AuthStore(tmp_path, b"test-secret-32bytes-test-secret!")
        assert store2.refresh(refresh)[0] == dev.device_id
