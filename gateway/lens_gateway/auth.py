"""设备配对与凭证：一次性配对码 → 设备注册 → 短时效 accessToken (JWT) + refreshToken。

红队 R8 铁律：OpenClaw 网关 token 永不出服务器。手机端只持有这里发行的设备凭证，
泄漏后单设备吊销即可，不影响全局。refreshToken 仅存哈希。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import jwt

ACCESS_TTL = 15 * 60          # accessToken 15 分钟
PAIR_CODE_TTL = 10 * 60       # 配对码 10 分钟


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Device:
    device_id: str
    name: str
    refresh_hash: str
    created_at: float
    revoked: bool = False


class AuthStore:
    def __init__(self, state_dir: Path, secret: bytes):
        self._path = state_dir / "devices.json"
        self._secret = secret
        self._devices: dict[str, Device] = {}
        self._pair_codes: dict[str, float] = {}  # code -> expires_at
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            self._devices = {k: Device(**v) for k, v in raw.get("devices", {}).items()}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"devices": {k: v.__dict__ for k, v in self._devices.items()}}
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        self._path.chmod(0o600)

    # ---- 配对 ----

    def new_pair_code(self) -> str:
        code = f"{secrets.randbelow(1000000):06d}"
        self._pair_codes[code] = time.time() + PAIR_CODE_TTL
        return code

    def pair(self, code: str, device_name: str) -> tuple[Device, str] | None:
        exp = self._pair_codes.get(code)
        if exp is None or exp < time.time():
            return None
        del self._pair_codes[code]  # 一次性
        device_id = "dev_" + secrets.token_hex(6)
        refresh = secrets.token_urlsafe(32)
        dev = Device(device_id=device_id, name=device_name[:64], refresh_hash=_sha256(refresh), created_at=time.time())
        self._devices[device_id] = dev
        self._save()
        return dev, refresh

    # ---- 凭证 ----

    def issue_access(self, device_id: str) -> tuple[str, int]:
        exp = int(time.time()) + ACCESS_TTL
        token = jwt.encode({"sub": device_id, "exp": exp, "scope": "lens"}, self._secret, algorithm="HS256")
        return token, exp

    def verify_access(self, token: str) -> str | None:
        """返回 device_id；过期抛 jwt.ExpiredSignatureError，其余无效返回 None。"""
        payload = jwt.decode(token, self._secret, algorithms=["HS256"])
        device_id = payload.get("sub", "")
        dev = self._devices.get(device_id)
        if dev is None or dev.revoked:
            return None
        return device_id

    def refresh(self, refresh_token: str) -> tuple[str, str, int] | None:
        """refreshToken 换新 accessToken。返回 (device_id, access, exp)。"""
        h = _sha256(refresh_token)
        for dev in self._devices.values():
            if dev.refresh_hash == h and not dev.revoked:
                access, exp = self.issue_access(dev.device_id)
                return dev.device_id, access, exp
        return None

    def revoke(self, device_id: str) -> bool:
        dev = self._devices.get(device_id)
        if dev:
            dev.revoked = True
            self._save()
            return True
        return False

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())
