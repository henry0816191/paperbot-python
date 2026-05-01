# Server Setup — Ubuntu 22.04

Step-by-step guide for provisioning a fresh Ubuntu 22.04 server to run
paperscout alongside other apps that share the same PostgreSQL and nginx.

---

## 1. System basics

```bash
sudo apt update && sudo apt upgrade -y

# Harden SSH: disable password auth
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

---

## 2. Docker Engine + Compose plugin

```bash
# Install prerequisites
sudo apt install -y ca-certificates curl gnupg

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add the repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Let the deploy user run docker without sudo
sudo usermod -aG docker gcp-cppdigest
newgrp docker
```

---

## 3. PostgreSQL 16 (shared instance)

If PostgreSQL is already running for other apps, skip the install and jump
to **Create the paperscout database**.

```bash
# Add PGDG repo
sudo apt install -y wget
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
  sudo apt-key add -
sudo apt update
sudo apt install -y postgresql-16

# Start and enable
sudo systemctl enable --now postgresql
```

### Create the paperscout database

```bash
sudo -u postgres psql <<'SQL'
CREATE USER paperscout WITH PASSWORD <password>;
CREATE DATABASE paperscout OWNER paperscout;
SQL
```

### Migrate data from an existing deployment (optional)

If you are replacing an old server that already has a running paperscout
database, dump it on the **old** server and restore it on the new one:

```bash
# --- On the OLD server ---
pg_dump -U postgres -Fc paperscout > /tmp/paperscout.dump
# or on Windows
"C:/Program Files/PostgreSQL/18/bin/pg_dump" -U postgres -Fc paperscout > paperscout.dump

# Copy the dump to the new server
scp /tmp/paperscout.dump <user>@<new-server>:/tmp/paperscout.dump
```

```bash
# --- On the NEW server (after creating the database above) ---
pg_restore -U paperscout -d paperscout --no-owner /tmp/paperscout.dump
rm /tmp/paperscout.dump
```

If the dump is stored in GCS (from the daily backup workflow),
download it directly on the new server instead:

```bash
gsutil cp gs://paperscout-backups/paperscout-<YYYYMMDD>.dump /tmp/paperscout.dump
pg_restore -U paperscout -h localhost -d paperscout --no-owner /tmp/paperscout.dump
rm /tmp/paperscout.dump
```

### Allow Docker containers to connect

Docker containers reach the host via `host.docker.internal`, which
resolves to the Docker gateway IP (typically `172.17.0.1`). PostgreSQL
must accept connections from that subnet.

```bash
# postgresql.conf — listen on all interfaces (or at least 172.17.0.1)
sudo sed -i "s/^#listen_addresses.*/listen_addresses = '*'/" \
  /etc/postgresql/16/main/postgresql.conf

# pg_hba.conf — allow the Docker bridge subnet
echo "host  paperscout  paperscout  172.16.0.0/12  scram-sha-256" | \
  sudo tee -a /etc/postgresql/16/main/pg_hba.conf

sudo systemctl restart postgresql
```

> **Note:** `172.16.0.0/12` covers all default Docker networks
> (`172.16.0.0` – `172.31.255.255`). Tighten this if you know the exact
> bridge subnet (`docker network inspect bridge`).

---

## 4. nginx + TLS

```bash
sudo apt install -y nginx

# Obtain a Let's Encrypt certificate (skip if already done for this domain)
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d dev.cppdigest.org
```

Certbot creates a server block for `dev.cppdigest.org` in the default
nginx config. Add the paperscout location blocks **inside that existing
server block** (do NOT create a separate server block -- nginx will
ignore it in favour of the first match).

Open the config and find the `dev.cppdigest.org` server block with
`listen 443 ssl;`. Add these lines before its closing `}`:

```nginx
    # --- paperscout ---
    location /paperscout/health {
        proxy_pass http://127.0.0.1:9101/health;
    }

    location /paperscout/ {
        proxy_pass http://127.0.0.1:9100/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

A reference copy of these blocks lives in
[`deploy/paperscout.conf`](paperscout.conf).

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 5. App deployment directory

Clone the repo into `/opt/paperscout`:

```bash
sudo mkdir -p /opt
sudo git clone https://github.com/cppalliance/paperscout-python.git /opt/paperscout
sudo chown -R gcp-cppdigest:gcp-cppdigest /opt/paperscout
```

Create the `.env` file:

```bash
cd /opt/paperscout
cp .env.example .env
# Edit with real credentials:
#   SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, DATABASE_URL, NOTIFICATION_CHANNEL
nano .env
```

The `DATABASE_URL` should use `host.docker.internal`:

```
DATABASE_URL=postgresql://paperscout:<password>@host.docker.internal:5432/paperscout
```

> **Note:** If the password contains special characters, they must be
> percent-encoded in the URL (e.g. `@` → `%40`, `!` → `%21`).
> Use `python3 -c "import urllib.parse; print(urllib.parse.quote('<password>', safe=''))"` to encode it.

---

## 6. First launch

```bash
cd /opt/paperscout
docker compose up -d --build

# Verify
sleep 5
curl -sf http://localhost:9101/health | python3 -m json.tool
docker compose logs -f paperscout
```

---

## 7. Restoring from a GCS backup (optional)

If migrating from another server with an existing database:

```bash
gsutil cp gs://paperscout-backups/paperscout-<YYYYMMDD>.dump /tmp/paperscout.dump
pg_restore -U paperscout -h localhost -d paperscout -c /tmp/paperscout.dump
rm /tmp/paperscout.dump
```

---

## 8. Database backups

The `db-backup.yml` GitHub Actions workflow SSHes into the server daily
and runs `pg_dump` + `gsutil cp` to upload to GCS. The VM's service
account handles authentication automatically — no credentials needed.

The GCS bucket `paperscout-backups` should have a lifecycle rule to
auto-delete objects older than 30 days (configured in the Cloud Console
under the bucket's **Lifecycle** tab).

---

## 9. GitHub Secrets checklist

Configure these in the repo under **Settings → Secrets and variables → Actions**:

| Secret           | Purpose                             |
| ---------------- | ----------------------------------- |
| `SERVER_HOST`    | Server IP or hostname               |
| `SERVER_USER`    | SSH username (e.g. `gcp-cppdigest`) |
| `SERVER_SSH_KEY` | Private SSH key for the deploy user |
| `SERVER_PORT`    | SSH port (optional, defaults to 22) |

`GITHUB_TOKEN` is provided automatically by GitHub Actions.
GCS authentication uses the VM's service account — no extra secrets needed.
