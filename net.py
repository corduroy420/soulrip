"""HTTPS settings shared by every web request the app makes.

A packaged app carries its own Python, whose default certificate location points at a folder on the
build machine. Using certifi's bundled certificate list makes HTTPS work on every computer.
"""
import os
import ssl

try:
    import certifi
    CA_FILE = certifi.where()
except ImportError:  # running from source without certifi: fall back to the system's certificates
    CA_FILE = None

SSL_CONTEXT = ssl.create_default_context(cafile=CA_FILE)


def configure_certificates():
    """Point libraries that build their own SSL context (and child processes) at the same certificates."""
    if CA_FILE:
        os.environ.setdefault("SSL_CERT_FILE", CA_FILE)
