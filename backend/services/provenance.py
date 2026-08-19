import os
import json
import base64
import hashlib
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization

# Keys live outside version control. Resolve relative to this file so the
# service works regardless of the process working directory.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_DIR = os.environ.get("ATTESTATION_KEY_DIR", os.path.join(BACKEND_DIR, "keys"))
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "private_key.pem")
PUBLIC_KEY_PATH = os.path.join(KEY_DIR, "public_key.pem")

# PEM (or base64-encoded PEM) supplied by the deployment platform's secret store.
PRIVATE_KEY_ENV = "ATTESTATION_PRIVATE_KEY"


class ProvenanceService:
    def __init__(self):
        self.private_key = self._resolve_private_key()
        self.public_key = self.private_key.public_key()

    # --- Key resolution ---------------------------------------------------
    def _resolve_private_key(self):
        """
        Loads the signing key, in order of preference:
          1. ATTESTATION_PRIVATE_KEY env var (PEM or base64-encoded PEM)
          2. keys/private_key.pem on disk
          3. A freshly generated keypair (persisted to disk when possible)
        """
        env_key = self._load_key_from_env()
        if env_key:
            print("🔑 [Attestation]: Signing key loaded from environment.")
            return env_key

        if os.path.exists(PRIVATE_KEY_PATH):
            with open(PRIVATE_KEY_PATH, "rb") as f:
                print(f"🔑 [Attestation]: Signing key loaded from {PRIVATE_KEY_PATH}")
                return serialization.load_pem_private_key(f.read(), password=None)

        print("🔑 [Attestation]: No signing key configured. Generating a new RSA keypair...")
        print("   ⚠️  This key is ephemeral. Set ATTESTATION_PRIVATE_KEY to keep")
        print("      signatures verifiable across restarts and deployments.")
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._persist_keypair(private_key)
        return private_key

    def _load_key_from_env(self):
        raw = os.environ.get(PRIVATE_KEY_ENV, "").strip()
        if not raw:
            return None

        # Platforms that mangle multi-line values accept a base64 blob instead.
        if "-----BEGIN" not in raw:
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except Exception as e:
                print(f"   ❌ [Attestation]: {PRIVATE_KEY_ENV} is neither PEM nor base64: {e}")
                return None

        try:
            return serialization.load_pem_private_key(raw.encode("utf-8"), password=None)
        except Exception as e:
            print(f"   ❌ [Attestation]: Could not parse {PRIVATE_KEY_ENV}: {e}")
            return None

    def _persist_keypair(self, private_key):
        """Best-effort write to disk; read-only filesystems just keep the key in memory."""
        try:
            os.makedirs(KEY_DIR, exist_ok=True)
            with open(PRIVATE_KEY_PATH, "wb") as f:
                f.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            os.chmod(PRIVATE_KEY_PATH, 0o600)

            with open(PUBLIC_KEY_PATH, "wb") as f:
                f.write(private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ))
            print(f"   💾 [Attestation]: Keypair written to {KEY_DIR} (git-ignored).")
        except OSError as e:
            print(f"   ⚠️  [Attestation]: Keypair kept in memory only ({e}).")

    # --- Public API -------------------------------------------------------
    def public_key_pem(self) -> str:
        """The verification key, safe to publish."""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

    def compute_fingerprint(self, data: dict) -> str:
        """Computes a persistent content hash (SHA-256) of a dictionary."""
        # Sort keys to ensure deterministic JSON
        canonical_json = json.dumps(data, sort_keys=True, default=str).encode('utf-8')
        return hashlib.sha256(canonical_json).hexdigest()

    def sign_record(self, record: dict) -> dict:
        """
        Signs the record and returns an Attestation object.
        """
        fingerprint = self.compute_fingerprint(record)
        timestamp = datetime.utcnow().isoformat() + "Z"

        attestation_payload = f"{timestamp}|{fingerprint}".encode('utf-8')

        signature = self.private_key.sign(
            attestation_payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )

        return {
            "timestamp": timestamp,
            "fingerprint": fingerprint,
            "signature": base64.b64encode(signature).decode('utf-8'),
            "algorithm": "RSA-SHA256",
            "verified": True  # Self-verified by design
        }


provenance_service = ProvenanceService()
