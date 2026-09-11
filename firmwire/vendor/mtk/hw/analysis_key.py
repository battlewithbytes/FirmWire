"""Private virtual-device identity. Not a vendor HUK or attestation key."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import uuid


class AnalysisKey:
    SCHEMA = "firmwire.synthetic-sej-key/v1"

    def __init__(self, root, identity):
        if len(root) != 32:
            raise ValueError("Synthetic SEJ root must be 32 bytes")
        self._root = root
        self.identity = str(uuid.UUID(identity))

    @classmethod
    def load_or_create(cls, path):
        """Publish atomically; never overwrite even an invalid existing identity.

        Identity belongs to a virtual device, not one firmware revision. Keep
        this private file with that device's NVRAM across upgrades and restarts.
        """
        path = Path(path)
        if not path.parent.is_dir():
            raise ValueError("Create the private SEJ state directory first")
        if not os.path.lexists(path):
            record = {"schema": cls.SCHEMA, "identity": str(uuid.uuid4()),
                      "root": secrets.token_hex(32)}
            fd, temporary = tempfile.mkstemp(prefix=".sej-key-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()):
                raise ValueError("SEJ state must be an owner-only regular file (0600)")
            try:
                data = stream.read(4097)
                if len(data) > 4096:
                    raise ValueError()
                record = json.loads(data)
                if set(record) != {"schema", "identity", "root"} or record["schema"] != cls.SCHEMA:
                    raise ValueError()
                return cls(bytes.fromhex(record["root"]), record["identity"])
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ValueError("Invalid SEJ identity; refusing to replace it") from None

    def derive(self, otp, length):
        if len(otp) != 32 or length not in (16, 24, 32):
            raise ValueError("Unsupported SEJ key derivation parameters")
        # Our versioned derivation, not a claim about MTK silicon.
        return hmac.new(self._root, b"FirmWire SEJ analysis v1\x00" + bytes([length]) + otp,
                        hashlib.sha256).digest()[:length]

    def __getstate__(self):
        # Snapshot storage is not guaranteed private and does not restore the
        # peripheral/guest transaction atomically. Never serialize this root.
        raise TypeError("Synthetic SEJ identity cannot be snapshotted; use a cold restart")

    def facts(self):
        return {"profile": "synthetic-sej-analysis-v1", "identity": self.identity,
                "synthetic": True, "vendor_huk": False, "attestation_supported": False,
                "derivation": "analysis-owned HMAC-SHA256; NOT vendor key derivation"}
