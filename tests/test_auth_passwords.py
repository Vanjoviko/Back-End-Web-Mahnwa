"""Uji verifikasi kata sandi kompatibel dengan akun pembaca lama (Node scrypt)."""
from __future__ import annotations

from app.auth import passwords

# Vektor nyata dari `crypto.scryptSync('secret123', salt, 64)` di Node.
NODE_RECORD = (
    "26cc8580ec2c5027a9a57081491551b5:"
    "b4d4d10793de7cd67bb1aad275766c449d428b8f677861a23009902b7a87caf1"
    "c14d1978272cd7a8c5c94ff146299160b824e0c9923ee3ce562ac687e4896253"
)


def test_node_scrypt_hash_verifies_in_python():
    assert passwords.verify_password("secret123", NODE_RECORD) is True


def test_wrong_password_rejected():
    assert passwords.verify_password("secret124", NODE_RECORD) is False


def test_empty_and_malformed_records_rejected():
    assert passwords.verify_password("secret123", None) is False
    assert passwords.verify_password("secret123", "") is False
    assert passwords.verify_password("secret123", "tanpa-pemisah") is False
    assert passwords.verify_password("secret123", "zz:zz") is False
    assert passwords.verify_password("secret123", "00:0011") is False


def test_hash_password_round_trip_and_layout():
    record = passwords.hash_password("kata-sandi-rahasia")
    salt_hex, hash_hex = record.split(":")
    assert len(bytes.fromhex(salt_hex)) == 16
    assert len(bytes.fromhex(hash_hex)) == 64
    assert passwords.verify_password("kata-sandi-rahasia", record) is True
    assert passwords.verify_password("salah", record) is False


def test_min_password_length_constant():
    assert passwords.MIN_PASSWORD_LENGTH == 8
