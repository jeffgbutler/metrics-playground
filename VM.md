# Running the playground on an always-on VM

The whole stack runs on any Linux VM with Docker. Only two images are home-grown (`simulator` and `docs`); you
build those on your Mac and push them to Harbor. Everything else is a public image the VM pulls itself. The VM
also gets a clone of this repo, because Prometheus, the collector, Grafana and the labs read their config from it,
and the labs have you edit those files.

```
 Mac                                        Harbor / GitHub                        VM
 scripts/publish-images.sh ──push──►  harbor.jbcodes.net/library/  ──pull──►  docker compose up -d --no-build
 git push ─────────────────────────►  github.com/jeffgbutler/     ──clone──►  ~/metrics-playground (+ its own .env)
                                        metrics-playground
```

The VM's DNS name is `docker.jbcodes.net`; substitute another name if it ever moves.

## What the VM needs

* Docker Engine with the Compose plugin (`docker compose version` prints v2.x) and `git`. amd64 or arm64: the
  images are built for both.
* **2 vCPU, 4 GB RAM, 20 GB disk** is comfortable. The stack uses roughly 1–1.5 GB of memory. Prometheus keeps 15 days
  of history (about 1–2 GB of disk); change `--storage.tsdb.retention.time` in `docker-compose.yml` for more or less.
* Outbound HTTPS to `harbor.jbcodes.net`, `github.com` and `api.honeycomb.io`.
* A synced clock (chrony / systemd-timesyncd). Every metric is timestamped, and lab 06 plays with clock skew on
  purpose, so the host's own clock needs to be right.
* Free ports 3000, 8080, 8081, 9090, 9101–9118, 9201–9213, 4317, 4318, 8888, 13133, 55679.

## 1. Publish the images and push the repo (on your Mac)

```bash
docker login harbor.jbcodes.net
scripts/publish-images.sh 0.1.0
git push
```

The script builds `metrics-playground-simulator` and `metrics-playground-docs` for `linux/amd64` and `linux/arm64`
and pushes each with two tags, `0.1.0` and `latest`. `PUSH=false scripts/publish-images.sh test` does the same builds
without pushing, which is a quick way to check that everything still builds.

(If `buildx` ever says multi-platform builds aren't supported by the current driver, create a builder once with
`docker buildx create --name multiarch --driver docker-container --use` and run the script again. OrbStack's
default builder handles it without this.)

The VM only sees what's pushed to GitHub, so commit and push config or lab changes before pulling them on the VM.

## 2. Clone and configure (on the VM)

```bash
git clone https://github.com/jeffgbutler/metrics-playground.git ~/metrics-playground
cd ~/metrics-playground
cp .env.example .env
chmod 600 .env
```

The repo is public, so HTTPS needs no credentials. `.env` is in `.gitignore`: it stays on the VM, and `git pull`
never touches it.

Edit `.env`:

```bash
HONEYCOMB_API_KEY=...                                # ingest key for the environment you want the data in
PLAYGROUND_REGISTRY=harbor.jbcodes.net/library       # pull the two images instead of building them
PLAYGROUND_TAG=0.1.0
PLAYGROUND_BIND=0.0.0.0                              # listen on the VM's network interface, not just localhost
CADVISOR_CONTAINERD_SOCK=/run/containerd/containerd.sock   # right for Docker Engine on Linux
```

## 3. Start it

```bash
docker login harbor.jbcodes.net        # only if the library project is private
docker compose pull
docker compose up -d --no-build
```

`--no-build` makes Compose use the Harbor images and fail loudly if one is missing, instead of quietly building
from source. Every service has `restart: unless-stopped`, so after a VM reboot the stack comes back as long as
Docker starts at boot (`sudo systemctl enable docker`).

## 4. Use it

From any machine on your network:

| | |
|---|---|
| Lab workbook | http://docker.jbcodes.net:8081 |
| Control UI | http://docker.jbcodes.net:8080 |
| Grafana | http://docker.jbcodes.net:3000 |
| Prometheus | http://docker.jbcodes.net:9090 |
| Raw exposition | http://docker.jbcodes.net:9101/metrics ... |

The cross-links (control UI → Grafana, lab buttons → Explore/Prometheus) use whatever hostname you browsed to, so
they point at `docker.jbcodes.net` with no configuration. Nothing has authentication (Grafana users are anonymous
admins), which is fine on a private homelab network.

## 5. Check it

```bash
docker compose ps                                          # 7 services, all "Up"
docker compose exec simulator metricsim status
docker compose exec prometheus wget -qO- localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c   # expect 31 "up"
docker compose logs cadvisor | grep "docker container factory"                         # "...successfully"
docker compose logs otel-collector | grep -i "exporting failed"                        # expect nothing
```

If cAdvisor reports a failed docker factory, the containerd socket path is wrong for this host.
`ls /run/containerd/containerd.sock /var/run/docker/containerd/containerd.sock` shows which one exists; put that
one in `CADVISOR_CONTAINERD_SOCK` and run `docker compose up -d cadvisor`.

A bonus of a real Linux VM: node-exporter, cAdvisor and the collector's `hostmetrics` now describe a real host, not
a Mac's hidden Docker VM.

## Living with it

**Honeycomb volume.** An always-on playground sends data around the clock: roughly 110 data points/s via
`prometheus-scrape`, 200/s via `prometheus-federate` and 10–15/s via `otlp-push`. Between labs, switch off the paths
you aren't using, for example `PIPELINE_FEDERATE_EXPORTERS=[nop]` in `.env`, then `docker compose up -d otel-collector`.

**Driving scenarios from your Mac.** Use the control UI, or over SSH:

```bash
ssh docker.jbcodes.net 'cd ~/metrics-playground && docker compose exec -T simulator metricsim trigger memory_leak --duration 20m'
```

**Changing labs or configs.** Commit and push on the Mac, then `git pull` on the VM. (If you edit on the VM itself,
commit from there too, or the next `git pull` will conflict.) Then, depending on what changed:

| changed | apply with |
|---|---|
| `labs/*.md` | refresh the browser |
| `grafana/dashboards/*.json` | nothing (Grafana rescans every 30 s) |
| `prometheus/*.yml`, `prometheus/rules/*` | `docker compose exec prometheus wget -qO- --post-data= localhost:9090/-/reload` |
| `collector/config.yaml` or `.env` | `docker compose up -d otel-collector` (or the service whose settings changed) |
| `docker-compose.yml` | `docker compose up -d --no-build` |

**Shipping simulator or docs changes.** On the Mac, `scripts/publish-images.sh 0.1.1` and push the code. On the VM,
`git pull`, set `PLAYGROUND_TAG=0.1.1` in `.env`, then `docker compose pull && docker compose up -d --no-build`. To
roll back, set the previous tag and do the same. Keep the image tag and the checked-out code in step: the labs
describe the simulator's behaviour, and `docker-compose.yml` passes it settings.

**Starting over.** `docker compose down` stops everything and keeps Prometheus and Grafana data;
`docker compose down -v` also deletes that data.

## Troubleshooting

* **`pull access denied` / `unauthorized`**: `docker login harbor.jbcodes.net` on the VM, or make the Harbor project
  public.
* **`no matching manifest for linux/...`**: the image was pushed for one platform only. Re-run
  `scripts/publish-images.sh` (it always builds both).
* **The UIs don't load from another machine**: check `PLAYGROUND_BIND=0.0.0.0` in `.env` (the default,
  `127.0.0.1`, only answers on the VM itself), then `docker compose up -d --no-build`. Also check the VM's firewall
  (`ufw`/`firewalld`) allows the ports above.
* **Permission denied reading `/etc/prometheus/...` on RHEL/Fedora**: SELinux is blocking bind mounts. Add `,z` to the
  read-only repo mounts (`./prometheus:/etc/prometheus:ro,z` and the others), or run `chcon -Rt svirt_sandbox_file_t ~/metrics-playground`.
* **A port is already in use**: something else on the VM owns it (3000 and 8080 are popular). Change the left-hand
  side of that port mapping in `docker-compose.yml`. The UIs' cross-links assume the standard ports, so those links
  will need editing too.
