"""A small private certificate authority for stt-tts, built on the openssl CLI.

    stt-tts certs init                         create the CA (once)
    stt-tts certs issue ravensnest --server --host ravensnest --host 192.168.1.50
    stt-tts certs issue kitchen-mic            client certificate for a device
    stt-tts certs issue stt-tts --use-for zenoh  the module's own Zenoh certificate
    stt-tts certs trust llm                    make the module trust this CA for the LLM link
    stt-tts certs list

Layout under paths.pki_dir (default: <data store>/pki):
    ca.key  ca.crt  ca.srl
    issued/<name>/key.pem  cert.pem  ca.crt

Keys are ECDSA P-256 (supported by OpenSSL, rustls/Zenoh and Python's ssl).
Server certificates carry the hostnames and IPs clients will connect to
(subjectAltName); TLS clients check those, not the common name. Client
certificates carry the device name as the common name, which is what Zenoh's
access control matches (cert_common_names).

The CA key can mint certificates every part of the system will trust: it is
created owner-only (0600). Back it up somewhere safe; a scorched-earth
reinstall deletes the data store, and with it the CA.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CA_DAYS = 3650
LEAF_DAYS = 825
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


class PKIError(RuntimeError):
    pass


def pki_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["pki_dir"]).expanduser()


def _openssl(*args: str, cwd: Path | None = None) -> str:
    if not shutil.which("openssl"):
        raise PKIError("openssl is not installed (sudo apt install openssl)")
    result = subprocess.run(["openssl", *args], capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise PKIError(f"openssl {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def _new_key(path: Path) -> None:
    old = os.umask(0o077)
    try:
        _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                 "-out", str(path))
    finally:
        os.umask(old)
    _private(path)


def init_ca(cfg: dict, common_name: str = "stt-tts CA", days: int = CA_DAYS,
            force: bool = False) -> Path:
    root = pki_dir(cfg)
    ca_key, ca_crt = root / "ca.key", root / "ca.crt"
    if ca_crt.exists() and not force:
        raise PKIError(f"A CA already exists at {root}. Use --force to replace it "
                       "(every certificate it issued will stop being trusted).")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    _new_key(ca_key)
    _openssl("req", "-x509", "-new", "-key", str(ca_key), "-sha256", "-days", str(days),
             "-subj", f"/CN={common_name}", "-out", str(ca_crt),
             "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash")
    return ca_crt


def _alt_names(hosts: list[str]) -> str:
    entries = []
    for host in hosts:
        try:
            ipaddress.ip_address(host)
            entries.append(f"IP:{host}")
        except ValueError:
            entries.append(f"DNS:{host}")
    return ",".join(entries)


def issue(cfg: dict, name: str, server: bool = False, hosts: list[str] | None = None,
          days: int = LEAF_DAYS, force: bool = False) -> Path:
    """Issue a certificate. Returns the folder holding key.pem, cert.pem and ca.crt."""
    if not NAME_RE.match(name):
        raise PKIError("Name must be letters, digits, dot, dash or underscore (max 63).")
    root = pki_dir(cfg)
    ca_key, ca_crt = root / "ca.key", root / "ca.crt"
    if not ca_crt.exists():
        raise PKIError("No CA yet. Run: stt-tts certs init")
    hosts = list(hosts or [])
    if server and not hosts:
        hosts = [name]  # at least the name itself
    out = root / "issued" / name
    if out.exists() and not force:
        raise PKIError(f"'{name}' was already issued. Use --force to reissue it.")
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    key, csr, crt = out / "key.pem", out / "request.csr", out / "cert.pem"
    _new_key(key)
    _openssl("req", "-new", "-key", str(key), "-subj", f"/CN={name}", "-out", str(csr))

    usage = "serverAuth" if server else "clientAuth"
    ext = ["basicConstraints=critical,CA:FALSE",
           "keyUsage=critical,digitalSignature",
           f"extendedKeyUsage={usage}",
           "subjectKeyIdentifier=hash",
           "authorityKeyIdentifier=keyid"]
    if hosts:
        ext.append(f"subjectAltName={_alt_names(hosts)}")
    ext_file = out / "ext.cnf"
    ext_file.write_text("\n".join(ext) + "\n")
    _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
             "-CAcreateserial", "-CAserial", str(root / "ca.srl"), "-sha256",
             "-days", str(days), "-extfile", str(ext_file), "-out", str(crt))
    csr.unlink()
    ext_file.unlink()
    shutil.copy2(ca_crt, out / "ca.crt")
    return out


@dataclass
class CertInfo:
    name: str
    kind: str
    hosts: str
    expires: datetime
    path: Path


def describe(cert: Path) -> tuple[str, str, datetime]:
    text = _openssl("x509", "-in", str(cert), "-noout", "-enddate", "-ext",
                    "extendedKeyUsage,subjectAltName")
    end = re.search(r"notAfter=(.+)", text).group(1).strip()
    expires = datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    kind = "server" if "Server Authentication" in text else "client"
    sans = re.findall(r"(?:DNS|IP Address):([^,\s]+)", text)
    return kind, ", ".join(sans), expires


def list_issued(cfg: dict) -> list[CertInfo]:
    found = []
    for folder in sorted((pki_dir(cfg) / "issued").glob("*")):
        cert = folder / "cert.pem"
        if cert.exists():
            kind, hosts, expires = describe(cert)
            found.append(CertInfo(folder.name, kind, hosts, expires, folder))
    return found


def use_for(cfg: dict, target: str, folder: Path) -> list[str]:
    """Point the module's own TLS settings at an issued client certificate."""
    ca = str(pki_dir(cfg) / "ca.crt")
    if target == "zenoh":
        cfg["zenoh"]["tls"].update(enabled=True, root_ca=ca, cert=str(folder / "cert.pem"),
                                   key=str(folder / "key.pem"))
        return ["zenoh.tls.enabled", "zenoh.tls.root_ca", "zenoh.tls.cert", "zenoh.tls.key"]
    if target == "llm":
        cfg["llm"].update(ca_file=ca, client_cert=str(folder / "cert.pem"),
                          client_key=str(folder / "key.pem"))
        return ["llm.ca_file", "llm.client_cert", "llm.client_key"]
    raise PKIError(f"Unknown target '{target}' (zenoh or llm)")


def trust(cfg: dict, target: str) -> list[str]:
    """Make the module trust this CA for a link, without a client certificate."""
    ca = pki_dir(cfg) / "ca.crt"
    if not ca.exists():
        raise PKIError("No CA yet. Run: stt-tts certs init")
    if target == "llm":
        cfg["llm"]["ca_file"] = str(ca)
        return ["llm.ca_file"]
    if target == "zenoh":
        cfg["zenoh"]["tls"]["root_ca"] = str(ca)
        return ["zenoh.tls.root_ca"]
    raise PKIError(f"Unknown target '{target}' (zenoh or llm)")
