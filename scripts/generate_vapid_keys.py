"""
scripts/generate_vapid_keys.py
================================
Generates the one VAPID key pair this server needs to send Web Push.

Run once, from the project root:

    python scripts/generate_vapid_keys.py

Then copy the two lines it prints into your .env file. Officers' devices
subscribe against the PUBLIC key, so changing the pair later invalidates
every existing subscription and every officer has to re-enable background
alerts - generate it once and keep it.

The private key is a server secret. Treat it exactly like SMTP_PASSWORD:
.env must never be committed.

Output format is the raw base64url encoding used by every Web Push library,
which is what pywebpush and the browser's PushManager both expect:
  public  - the 65-byte uncompressed P-256 point
  private - the 32-byte private scalar
"""

import base64
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def b64url(raw: bytes) -> str:
    """Base64url with padding stripped, as the Web Push spec requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> int:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        print(
            "The 'cryptography' package is required.\n"
            "It installs automatically with pywebpush:\n\n"
            "    pip install pywebpush\n",
            file=sys.stderr,
        )
        return 1

    # VAPID is defined over P-256 (prime256v1 / secp256r1) specifically; no
    # other curve is accepted by browser push services.
    private_key = ec.generate_private_key(ec.SECP256R1())

    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    public_b64 = b64url(public_raw)
    private_b64 = b64url(private_raw)

    print("\nVAPID key pair generated.\n")
    print("Add these three lines to your .env file:\n")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print("VAPID_SUBJECT=mailto:you@example.com")
    print(
        "\nSet VAPID_SUBJECT to a real contact address: push services use it "
        "to reach you if this server starts misbehaving."
    )

    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        print(f"\nYour .env file is at: {env_path}")
    else:
        print(
            f"\nNo .env file found at {env_path} yet - copy .env.example to "
            ".env first, then add the lines above."
        )

    print(
        "\nKeep VAPID_PRIVATE_KEY secret and out of version control. "
        "Anyone holding it can send push notifications as this server.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
