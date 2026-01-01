#!/usr/bin/env python3
"""
SMTP Configuration Debug Script

Tests your SMTP credentials and sends a test email.
Usage: python scripts/smtp_debug.py
"""

import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def load_env():
    """Load environment variables from .env file."""
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        print(f"✓ Found .env file at {env_file}")
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key] = value.strip('"').strip("'")
    else:
        print(f"✗ No .env file found at {env_file}")
        return False
    return True


def get_smtp_config():
    """Get SMTP configuration from environment."""
    config = {
        "host": os.getenv("SMTP_HOST"),
        "port": os.getenv("SMTP_PORT"),
        "user": os.getenv("SMTP_USER"),
        "password": os.getenv("SMTP_PASSWORD"),
    }

    print("\n📧 SMTP Configuration:")
    print(f"  Host: {config['host']}")
    print(f"  Port: {config['port']}")
    print(f"  User: {config['user']}")
    print(f"  Password: {'*' * len(config['password']) if config['password'] else 'NOT SET'}")

    missing = [k for k, v in config.items() if not v]
    if missing:
        print(f"\n✗ Missing configuration: {', '.join(missing)}")
        return None

    return config


def test_smtp_connection(config):
    """Test SMTP connection and capabilities."""
    print("\n🔍 Testing SMTP Connection...")

    try:
        port = int(config["port"])
        print(f"\n1️⃣ Attempting to connect to {config['host']}:{port}...")

        # Try with SMTP (not SMTP_SSL)
        server = smtplib.SMTP(config["host"], port, timeout=10)
        print("   ✓ Connection established")

        # Enable debug output
        server.set_debuglevel(1)

        print("\n2️⃣ Sending EHLO...")
        server.ehlo()
        print("   ✓ EHLO successful")

        print("\n3️⃣ Checking if STARTTLS is supported...")
        if server.has_extn("STARTTLS"):
            print("   ✓ STARTTLS supported")
            print("\n4️⃣ Starting TLS...")
            server.starttls()
            print("   ✓ TLS started")
            server.ehlo()  # Re-identify after STARTTLS
        else:
            print("   ⚠ STARTTLS not supported")

        print("\n5️⃣ Attempting login...")
        server.login(config["user"], config["password"])
        print("   ✓ Login successful")

        server.quit()
        print("\n✅ All connection tests passed!")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"\n✗ Authentication failed: {e}")
        print("\nPossible fixes:")
        print("  - Check username and password")
        print("  - Enable 'Less secure app access' (Gmail)")
        print("  - Use an app-specific password (recommended)")
        return False

    except smtplib.SMTPServerDisconnected as e:
        print(f"\n✗ Connection unexpectedly closed: {e}")
        print("\nPossible fixes:")
        print("  - Check if SMTP_HOST is correct")
        print("  - Try using SMTP_SSL on port 465 instead of SMTP on port 587")
        print("  - Check if your IP is blocked by the mail server")
        print("  - Verify firewall settings")
        return False

    except Exception as e:
        print(f"\n✗ Connection failed: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_smtp_ssl_connection(config):
    """Test SMTP_SSL connection (port 465)."""
    print("\n🔍 Testing SMTP_SSL Connection (Alternative Method)...")

    try:
        # Try with port 465 and SSL
        port = 465
        print(f"\n1️⃣ Attempting SSL connection to {config['host']}:{port}...")

        server = smtplib.SMTP_SSL(config["host"], port, timeout=10)
        print("   ✓ SSL connection established")

        print("\n2️⃣ Attempting login...")
        server.login(config["user"], config["password"])
        print("   ✓ Login successful")

        server.quit()
        print("\n✅ SSL connection tests passed!")
        print("\n💡 Suggestion: Update your .env to use:")
        print("   SMTP_PORT=465")
        print("   And update emails.py to use smtplib.SMTP_SSL instead of smtplib.SMTP")
        return True

    except Exception as e:
        print(f"\n✗ SSL connection failed: {type(e).__name__}: {e}")
        return False


def send_test_email(config):
    """Send a test email."""
    print("\n📨 Sending Test Email...")

    to_email = "amanchoudhri@gmail.com"

    # Create message
    msg = MIMEText("This is a test email from the Blogregator SMTP debug script.", "plain")
    msg["Subject"] = "🔧 Blogregator SMTP Test"
    msg["From"] = f"Blogregator <{config['user']}>"
    msg["To"] = to_email

    try:
        # Send using STARTTLS (port 587)
        port = int(config["port"])
        print(f"Connecting to {config['host']}:{port}...")

        with smtplib.SMTP(config["host"], port, timeout=10) as server:
            server.starttls()
            server.login(config["user"], config["password"])
            server.send_message(msg)

        print(f"\n✅ Test email sent successfully to {to_email}!")
        return True

    except Exception as e:
        print(f"\n✗ Failed to send test email: {e}")

        # Try with SSL as alternative
        print("\n🔄 Trying with SMTP_SSL (port 465)...")
        try:
            with smtplib.SMTP_SSL(config["host"], 465, timeout=10) as server:
                server.login(config["user"], config["password"])
                server.send_message(msg)

            print(f"\n✅ Test email sent successfully to {to_email} using SSL!")
            print("\n💡 Your SMTP works with SSL on port 465.")
            print("   Update your configuration to use port 465.")
            return True
        except Exception as e2:
            print(f"\n✗ SSL attempt also failed: {e2}")
            return False


def main():
    """Main function."""
    print("=" * 60)
    print("🔧 Blogregator SMTP Debug Script")
    print("=" * 60)

    # Load environment
    if not load_env():
        print("\n❌ Could not load .env file")
        return 1

    # Get config
    config = get_smtp_config()
    if not config:
        print("\n❌ Invalid SMTP configuration")
        return 1

    # Test connection
    connection_ok = test_smtp_connection(config)

    # If STARTTLS failed, try SSL
    if not connection_ok:
        ssl_ok = test_smtp_ssl_connection(config)
        if not ssl_ok:
            print("\n❌ Both STARTTLS and SSL connections failed")
            return 1

    # Send test email
    print("\n" + "=" * 60)
    if input("\nSend a test email? (y/n): ").lower() == "y":
        send_test_email(config)

    print("\n" + "=" * 60)
    print("✅ Debug complete!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
