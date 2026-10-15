# Install inside WSL

Use this if you prefer working in a Linux shell on Windows. The containers still run in Docker
Desktop; only the checkout and the CLI live in your WSL distribution.

1. Install Docker Desktop as described in [windows.md](windows.md).
2. In Docker Desktop, open **Settings → Resources → WSL integration** and enable your distribution
   (for example Ubuntu).
3. In the WSL terminal, confirm Docker works: `docker compose version`.
4. Install:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
   ```

5. Open http://localhost:3000 in your Windows browser.

## Notes

- Keep the checkout inside the Linux filesystem (`~/opencortyx`), not under `/mnt/c`. File access
  across the Windows boundary is much slower, especially for development mode.
- GPU support comes from the Windows NVIDIA driver; nothing needs to be installed inside WSL.
- Docker Engine installed directly inside WSL (without Docker Desktop) also works; follow
  [linux.md](linux.md) in that case.
