# Run on a server

This guide deploys OpenCortyx on a single Linux VM with HTTPS. It assumes Ubuntu 22.04 or 24.04 and a
domain you control.

## 1. Size the machine

| Model backend | vCPU | Memory | Disk |
|---|---|---|---|
| Hosted API (OpenAI, Anthropic, Azure, …) | 4 | 16 GB | 50 GB |
| Local vLLM on an NVIDIA GPU | 8 | 32 GB | 100 GB |

## 2. Install Docker

Follow [linux.md](linux.md) steps 1-2 (and the GPU step if you use vLLM).

## 3. Point DNS at the server

Create three records for the server's public IP, for example:

- `opencortyx.example.com` (dashboard)
- `api.opencortyx.example.com` (REST API)
- `mcp.opencortyx.example.com` (MCP server for AI clients)

## 4. Install and configure OpenCortyx

```bash
curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
```

Edit `~/opencortyx/.env`:

```env
PUBLIC_WEB_URL=https://opencortyx.example.com
PUBLIC_API_URL=https://api.opencortyx.example.com
MCP_ALLOWED_HOSTS=mcp.opencortyx.example.com,mcp.opencortyx.example.com:*,localhost:*,127.0.0.1:*
```

Apply it. A custom `PUBLIC_API_URL` makes `up` build the web image, because the URL is compiled into
the dashboard:

```bash
opencortyx up
```

Services stay bound to `127.0.0.1`; only the reverse proxy is exposed.

## 5. Add HTTPS with Caddy

```bash
sudo apt-get install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
opencortyx.example.com {
    reverse_proxy 127.0.0.1:3000
}
api.opencortyx.example.com {
    reverse_proxy 127.0.0.1:8000
}
mcp.opencortyx.example.com {
    reverse_proxy 127.0.0.1:8001
}
EOF
sudo systemctl reload caddy
```

Caddy obtains and renews certificates automatically. Open https://opencortyx.example.com and create
the admin account immediately: the first visitor to an empty installation becomes the admin.

## 6. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

## 7. Backups

Schedule a daily database dump and keep copies off the server:

```bash
crontab -e
# 30 2 * * * cd ~/opencortyx && bin/opencortyx backup /var/backups/opencortyx >> ~/opencortyx-backup.log 2>&1
```

See [Backup and restore](../BACKUP-RESTORE.md).

## 8. Operations

- **Updates:** `opencortyx update`; migrations run before the API starts.
- **Health:** `opencortyx doctor`; `GET https://api.opencortyx.example.com/health`; Prometheus metrics at
  `/metrics` (restrict this path at the proxy if the API is public).
- **Logs:** `opencortyx logs <service>`; each container keeps 3 × 10 MB of rotated logs.
- **Memory:** see [memory-budget.md](../memory-budget.md) before changing concurrency.
