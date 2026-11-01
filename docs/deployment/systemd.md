# systemd Service (Linux)

Ghost includes a systemd unit file for running the API server as a managed background service on Linux. This provides automatic startup on boot, crash recovery, and integration with standard Linux service management tools.

## Prerequisites

Before installing the service, ensure that:

1. Ghost is installed in a virtual environment at `/opt/ghost/.venv` (or adjust paths accordingly).
2. A dedicated `ghost` system user exists (recommended for security).
3. An inference engine (such as Ollama) is running and accessible.

Create the user and installation directory:

```bash
sudo useradd --system --create-home --home-dir /opt/ghost ghost
sudo -u ghost python3 -m venv /opt/ghost/.venv
sudo -u ghost git clone https://github.com/hamz5ashah/Ghost.git /opt/ghost/Ghost
cd /opt/ghost/Ghost && sudo -u ghost uv sync --extra server
```

## Installing the Service

The unit binds `0.0.0.0`, so an **API key is required** — and the unit
declares `EnvironmentFile=/etc/ghost/env` (no `-` prefix), so it will
**fail to start** until that file exists with a key. Create it first:

```bash
sudo mkdir -p /etc/ghost
echo "GHOST_API_KEY=$(ghost auth generate-key)" | sudo tee /etc/ghost/env
sudo chmod 600 /etc/ghost/env
```

Then copy the unit file, reload the daemon, and enable the service:

```bash
sudo cp deploy/systemd/ghost.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ghost
sudo systemctl start ghost
```

Clients must send `Authorization: Bearer <key>` on `/v1/*` and `/api/*`
requests. (If you instead bind to `127.0.0.1`, the key is optional and you
can drop the `EnvironmentFile` line.)

Verify it is running:

```bash
sudo systemctl status ghost
```

## Service File Reference

The provided unit file at `deploy/systemd/ghost.service`:

```ini
[Unit]
Description=Ghost API Server
After=network.target

[Service]
Type=simple
User=ghost
WorkingDirectory=/opt/ghost
ExecStart=/opt/ghost/.venv/bin/ghost serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
Environment=HOME=/opt/ghost

[Install]
WantedBy=multi-user.target
```

### `[Unit]` Section

| Directive     | Value              | Description                                                                 |
|---------------|--------------------|-----------------------------------------------------------------------------|
| `Description` | `Ghost API Server` | Human-readable name shown in `systemctl status` and logs.              |
| `After`       | `network.target`   | Delays startup until the network stack is available, since the server binds to a network socket and may need to reach a remote engine. |

### `[Service]` Section

| Directive          | Value                                                              | Description                                                                                     |
|--------------------|--------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `Type`             | `simple`                                                           | The process started by `ExecStart` is the main service process. systemd considers the service started immediately. |
| `User`             | `ghost`                                                       | Runs the server as the `ghost` user rather than root, limiting the blast radius of any security issue. |
| `WorkingDirectory` | `/opt/ghost`                                                  | Sets the working directory for the process. This is where Ghost looks for local files and writes data. |
| `ExecStart`        | `/opt/ghost/.venv/bin/ghost serve --host 0.0.0.0 --port 8000` | The command to start the server. Uses the full path to the `ghost` binary inside the virtual environment. |
| `Restart`          | `on-failure`                                                       | Automatically restarts the service if it exits with a non-zero exit code. Does not restart on clean shutdown (`systemctl stop`). |
| `RestartSec`       | `5`                                                                | Waits 5 seconds before attempting a restart, preventing rapid restart loops if the service crashes immediately on startup. |
| `Environment`      | `HOME=/opt/ghost`                                             | Sets the `HOME` environment variable so Ghost finds its configuration at `~/.ghost/config.toml` (resolving to `/opt/ghost/.ghost/config.toml`). |

### `[Install]` Section

| Directive    | Value               | Description                                                                                 |
|--------------|---------------------|---------------------------------------------------------------------------------------------|
| `WantedBy`   | `multi-user.target` | The service starts when the system reaches multi-user mode (standard boot target for servers). `systemctl enable` creates a symlink under this target. |

## Configuration Options

### Changing the Bind Address and Port

Edit the `ExecStart` line to change the host or port:

```ini
ExecStart=/opt/ghost/.venv/bin/ghost serve --host 127.0.0.1 --port 9000
```

!!! tip
    Binding to `127.0.0.1` restricts access to localhost only. Use this when running behind a reverse proxy like Nginx or Caddy.

### Setting the Engine and Model

Pass additional flags to `ghost serve`:

```ini
ExecStart=/opt/ghost/.venv/bin/ghost serve --host 0.0.0.0 --port 8000 --engine ollama --model qwen3:8b
```

### Adding Environment Variables

Add multiple `Environment` directives or use `EnvironmentFile` for complex configurations:

```ini
[Service]
Environment=HOME=/opt/ghost
Environment=GHOST_ENGINE_DEFAULT=vllm
Environment=GHOST_OLLAMA_HOST=http://localhost:11434
```

Or load from a file:

```ini
[Service]
EnvironmentFile=/opt/ghost/.env
```

### Changing the User

If you prefer a different service user, update both the `User` directive and the paths:

```ini
[Service]
User=myuser
WorkingDirectory=/home/myuser/ghost
ExecStart=/home/myuser/ghost/.venv/bin/ghost serve --host 0.0.0.0 --port 8000
Environment=HOME=/home/myuser/ghost
```

### Using a Configuration File

Ensure the configuration file exists at the path where `HOME` points:

```bash
sudo -u ghost mkdir -p /opt/ghost/.ghost
sudo -u ghost cp config.toml /opt/ghost/.ghost/config.toml
```

The server reads `~/.ghost/config.toml` on startup, where `~` resolves from the `HOME` environment variable.

## Viewing Logs

Ghost logs are captured by journald. View them with `journalctl`:

```bash
# View all logs for the service
sudo journalctl -u ghost

# Follow logs in real time
sudo journalctl -u ghost -f

# View logs since the last boot
sudo journalctl -u ghost -b

# View logs from the last hour
sudo journalctl -u ghost --since "1 hour ago"

# View only error-level messages
sudo journalctl -u ghost -p err
```

## Managing the Service

### Start, Stop, and Restart

```bash
# Start the service
sudo systemctl start ghost

# Stop the service
sudo systemctl stop ghost

# Restart the service (stop + start)
sudo systemctl restart ghost

# Reload configuration without full restart (sends SIGHUP)
sudo systemctl reload-or-restart ghost
```

### Check Status

```bash
sudo systemctl status ghost
```

Example output:

```
● ghost.service - Ghost API Server
     Loaded: loaded (/etc/systemd/system/ghost.service; enabled; preset: enabled)
     Active: active (running) since Fri 2026-02-21 10:00:00 UTC; 2h ago
   Main PID: 12345 (ghost)
      Tasks: 4 (limit: 4915)
     Memory: 256.0M
        CPU: 1min 23s
     CGroup: /system.slice/ghost.service
             └─12345 /opt/ghost/.venv/bin/python /opt/ghost/.venv/bin/ghost serve --host 0.0.0.0 --port 8000
```

### Enable and Disable on Boot

```bash
# Enable automatic start on boot
sudo systemctl enable ghost

# Disable automatic start on boot
sudo systemctl disable ghost
```

### Apply Changes After Editing the Unit File

After modifying `/etc/systemd/system/ghost.service`, reload the systemd daemon and restart the service:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ghost
```

## Running Alongside Ollama

If Ollama is also managed via systemd, you can add an ordering dependency so the Ghost service waits for Ollama to start:

```ini
[Unit]
Description=Ghost API Server
After=network.target ollama.service
Requires=ollama.service
```

| Directive  | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `After`    | Ensures Ghost starts after Ollama.                                  |
| `Requires` | If Ollama fails to start, Ghost will not start either.              |

!!! note
    Use `Wants` instead of `Requires` if you want Ghost to start even when Ollama is unavailable (for example, if you plan to start Ollama manually later).
