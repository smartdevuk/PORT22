"""SSH identity loading and verified SFTP sessions."""

import base64
import hashlib
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading

import paramiko
from cryptography.hazmat.primitives import serialization as keys
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa


def fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def load_key(filename, ask_secret):
    file = Path(filename).expanduser()
    with file.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("This file is too large to be an SSH private key.")
    if raw.startswith(b"PuTTY-User-Key-File-"):
        encrypted = not re.search(rb"(?m)^Encryption: none\r?$", raw)
        secret = ask_secret() if encrypted else ""
        result = subprocess.run(
            [
                "puttygen",
                str(file.resolve()),
                "-O",
                "private-openssh-new",
                "-o",
                "/dev/stdout",
                "--old-passphrase",
                "/dev/stdin",
                "--new-passphrase",
                "/dev/null",
            ],
            input=(secret + "\n").encode(),
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise ValueError(
                "Could not unlock the PuTTY key. Check its passphrase and file format."
            )
        raw = result.stdout
    loader = (
        keys.load_ssh_private_key
        if b"BEGIN OPENSSH PRIVATE KEY" in raw
        else keys.load_pem_private_key
    )
    if b"PRIVATE KEY-----" not in raw:
        raise ValueError(
            "Choose the private key file. A .pub file only contains the public key."
        )
    try:
        private = loader(raw, password=None)
    except (TypeError, ValueError) as error:
        if not any(
            word in str(error).lower()
            for word in ("password", "encrypted", "protected")
        ):
            raise ValueError(
                "This private key could not be read. Choose an OpenSSH, PEM or PuTTY key."
            ) from error
        try:
            private = loader(raw, password=ask_secret().encode())
        except (TypeError, ValueError) as wrong:
            raise ValueError(
                "Could not unlock the private key. Check the passphrase."
            ) from wrong
    for kind, implementation in (
        (rsa.RSAPrivateKey, paramiko.RSAKey),
        (ed25519.Ed25519PrivateKey, paramiko.Ed25519Key),
        (ec.EllipticCurvePrivateKey, paramiko.ECDSAKey),
    ):
        if isinstance(private, kind):
            data = private.private_bytes(
                keys.Encoding.PEM, keys.PrivateFormat.OpenSSH, keys.NoEncryption()
            )
            return implementation.from_private_key(io.StringIO(data.decode()))
    raise ValueError("Choose an Ed25519, RSA or ECDSA SSH key.")


class Trust(paramiko.MissingHostKeyPolicy):
    def __init__(self, filename, ask):
        self.filename = filename
        self.ask = ask

    def missing_host_key(self, client, hostname, key):
        if self.ask is None or not self.ask(hostname, key.get_name(), fingerprint(key)):
            raise ValueError(
                "Connection stopped. The server fingerprint was not trusted."
            )
        known = client.get_host_keys()
        known.add(hostname, key.get_name(), key)
        fd, name = tempfile.mkstemp(dir=self.filename.parent)
        os.close(fd)
        try:
            known.save(name)
            os.replace(name, self.filename)
        finally:
            Path(name).unlink(missing_ok=True)


class Session:
    def __init__(self, profile, identity, directory):
        self.profile, self.identity, self.directory = profile, identity, directory
        self.lock = threading.Lock()
        self.client = None
        self.closed = False

    def connect(self, ask=None):
        with self.lock:
            if self.closed:
                raise ConnectionError("Disconnected.")
            if self.client:
                t = self.client.get_transport()
                if t and t.is_active() and t.is_authenticated():
                    return t
                self.client.close()
            known = self.directory / "known_hosts"
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            if known.is_file():
                client.load_host_keys(str(known))
            client.set_missing_host_key_policy(Trust(known, ask))
            try:
                client.connect(
                    self.profile["host"],
                    port=self.profile["port"],
                    username=self.profile["user"],
                    pkey=self.identity,
                    allow_agent=False,
                    look_for_keys=False,
                    timeout=12,
                    banner_timeout=15,
                    auth_timeout=18,
                )
                transport = client.get_transport()
                transport.set_keepalive(15)
                self.client = client
                return transport
            except BaseException:
                client.close()
                raise

    def channel(self):
        transport = self.connect()
        ch = transport.open_session(timeout=15)
        try:
            ch.settimeout(30)
            ch.invoke_subsystem("sftp")
            return paramiko.SFTPClient(ch)
        except BaseException:
            ch.close()
            raise

    def close(self):
        with self.lock:
            self.closed = True
            if self.client:
                self.client.close()
            self.identity = None


def describe(error):
    if isinstance(error, paramiko.BadHostKeyException):
        return (
            "Server identity has changed. Connection blocked. Verify this Droplet in the DigitalOcean "
            "console before updating its known_hosts entry."
        )
    if isinstance(error, paramiko.AuthenticationException):
        return "The server rejected this key for that username. Check the username and the selected private key."
    if isinstance(error, socket.gaierror):
        return "The hostname could not be found. Check the address and your connection."
    return str(error) or type(error).__name__
