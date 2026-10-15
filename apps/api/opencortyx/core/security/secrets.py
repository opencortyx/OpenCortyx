"""Optional symmetric encryption for secrets at rest (connector DSNs).

If `CONNECTOR_SECRET_KEY` is set AND `cryptography` is installed, secrets are Fernet-encrypted and
stored with an `enc:` prefix. Otherwise they're stored as-is (current behaviour) — so this degrades
gracefully and never blocks ingestion. Decryption transparently handles both forms.
"""
from opencortyx.core.config import settings
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

_PREFIX = "enc:"
_fernet = None
_init = False


def _get_fernet():
    global _fernet, _init
    if _init:
        return _fernet
    _init = True
    key = (settings.CONNECTOR_SECRET_KEY or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:               # missing lib / bad key → fall back to plaintext
        log.warning("secrets.encryption_disabled", error=str(e))
        _fernet = None
    return _fernet


def encrypt_secret(value: str) -> str:
    """Encrypt if possible; otherwise return the value unchanged."""
    if not value:
        return value
    f = _get_fernet()
    if not f:
        return value
    try:
        return _PREFIX + f.encrypt(value.encode()).decode()
    except Exception:
        return value


def decrypt_secret(value: str) -> str:
    """Decrypt values written by encrypt_secret; pass through anything not encrypted."""
    if not value or not value.startswith(_PREFIX):
        return value
    f = _get_fernet()
    if not f:
        return value          # cannot decrypt without the key — return as stored
    try:
        return f.decrypt(value[len(_PREFIX):].encode()).decode()
    except Exception:
        return value


# ── pluggable secret providers (DataHub-style) ────────────────────────────────
def _subst_env(s: str) -> str:
    """Replace ${ENV_VAR} references with environment values (DataHub recipe style)."""
    if not s or "${" not in s:
        return s
    import os
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), s)


def _azure_kv_get(vault_url: str, name: str) -> str:
    """Fetch a secret value from Azure Key Vault. Lazy-imports the Azure SDK so the app runs without
    it installed; raises a clear error (surfaced as a handled 400) when used but unavailable."""
    if not vault_url or not name:
        raise RuntimeError("azure_keyvault secret requires both vault_url and secret_name")
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except Exception as e:                       # SDK not installed
        raise RuntimeError("Azure Key Vault SDK not installed (pip install azure-keyvault-secrets "
                           f"azure-identity): {e}")
    client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    return client.get_secret(name).value or ""


def resolve_secret(spec) -> str:
    """Resolve a secret to its plaintext value. `spec` is either:
      • a str — a literal/inline-encrypted value (with ${ENV} substitution), or
      • a dict — {"provider": "inline"|"env"|"azure_keyvault", ...}:
          inline:         {"value": "enc:…|plaintext"}
          env:            {"var": "DB_PASSWORD"}
          azure_keyvault: {"vault_url": "https://…", "secret_name": "db-pass"}  (vault_url optional → default)
    The actual secret is fetched at runtime and never persisted in plaintext."""
    if spec is None:
        return ""
    if isinstance(spec, str):
        return _subst_env(decrypt_secret(spec))
    if isinstance(spec, dict):
        prov = (spec.get("provider") or "inline").lower()
        if prov == "inline":
            return _subst_env(decrypt_secret(spec.get("value") or ""))
        if prov == "env":
            import os
            return os.environ.get(spec.get("var") or "", "")
        if prov in ("azure_keyvault", "azure", "keyvault"):
            return _azure_kv_get(str(spec.get("vault_url") or settings.AZURE_KEYVAULT_URL),
                                 str(spec.get("secret_name") or spec.get("name") or ""))
        raise RuntimeError(f"unknown secret provider: {prov}")
    return ""


#: SQL connector kinds built from fields (host/port/database/user): SQLAlchemy scheme + default port.
#: One table so the builder and the request validator can never disagree about which kinds exist.
SQL_DIALECTS = {
    "postgres": ("postgresql", 5432),
    "mysql": ("mysql+pymysql", 3306),
    "mssql": ("mssql+pymssql", 1433),     # pymssql wheels bundle the TDS client; pyodbc needs system ODBC drivers the image lacks
    "oracle": ("oracle+oracledb", 1521),
}
#: File databases: the `database` field is the file's path.
FILE_DIALECTS = {"sqlite": "sqlite"}


def resolve_connector_dsn(config: dict) -> str:
    """Build the runtime DSN for a saved connector. Two shapes are supported:
      • field-based: host/port/database/username + a `secret` (password from KV/env/inline), or
      • connection_string: a raw/encrypted DSN, with ${ENV} and an optional ${SECRET} placeholder
        that is filled from the resolved `secret`.
    The stored config never holds a plaintext password."""
    config = config or {}
    secret = config.get("secret")
    host, database = config.get("host"), config.get("database")
    kind = (config.get("kind") or "postgres").lower()
    if kind in FILE_DIALECTS and database and not config.get("connection_string"):
        return f"{FILE_DIALECTS[kind]}:///{database}"
    if host and database:
        from urllib.parse import quote_plus
        scheme, default_port = SQL_DIALECTS.get(kind, SQL_DIALECTS["postgres"])
        port = config.get("port") or default_port
        user = config.get("username") or ""
        pw = resolve_secret(secret) if secret is not None else ""
        auth = (user + (f":{quote_plus(pw)}" if pw else "")) if user else ""
        at = f"{auth}@" if auth else ""
        return f"{scheme}://{at}{host}:{port}/{database}"
    cs = config.get("connection_string")
    if cs:
        dsn = _subst_env(decrypt_secret(cs))
        if "${SECRET}" in dsn and secret is not None:
            from urllib.parse import quote_plus
            dsn = dsn.replace("${SECRET}", quote_plus(resolve_secret(secret)))
        return dsn
    return ""
