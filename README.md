# TRMNL Proxmox Dashboard

A private TRMNL plugin that shows your EPYC Proxmox node's CPU/RAM/disk
usage, uptime, and a running/stopped list of VMs & LXCs. It works via
TRMNL's **Webhook** strategy: a small Python script polls Proxmox and
pushes a JSON payload to TRMNL; TRMNL renders it into the Liquid template
you paste into the plugin's Markup Editor.

```
Proxmox API  --(script, cron)-->  TRMNL webhook  --(Liquid template)-->  e-ink display
```

## 1. Create a read-only Proxmox API token

In the Proxmox web UI:

1. **Datacenter → Permissions → Users** → Add a user, e.g. `trmnl@pve`
   (no password needed — it'll only ever use an API token).
2. **Datacenter → Permissions → API Tokens** → Add. User = `trmnl@pve`,
   Token ID = `trmnl-token`, **uncheck** "Privilege Separation" only if you
   want the token to inherit the user's permissions directly — either way
   works, just be consistent with step 3.
3. **Datacenter → Permissions → Add → User Permission**: Path `/nodes/<your-node>`,
   User `trmnl@pve`, Role `PVEAuditor` (built-in read-only role — this
   script never writes anything to Proxmox).
4. Copy the token secret shown — it's only displayed once.

## 2. Where to run the script

Recommended: a small, dedicated LXC container on the EPYC host (matches
how you already isolate services) rather than cron on the hypervisor
itself — keeps the Proxmox host's own OS untouched and the token scoped to
one throwaway container you can destroy/rebuild anytime.

```bash
# On the new LXC (Debian/Ubuntu base):
sudo apt update && sudo apt install -y python3-venv python3-pip
sudo mkdir -p /opt/trmnl-proxmox-plugin
cd /opt/trmnl-proxmox-plugin
# copy push_proxmox_stats.py, requirements.txt, .env.example here
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in PROXMOX_HOST, PROXMOX_NODE, token, TRMNL_PLUGIN_UUID
```

(If you'd rather run it directly on the Proxmox host or another
always-on box, the only thing that changes is the path in the systemd
unit / cron line below — the script itself doesn't care.)

Test it manually first:

```bash
./venv/bin/python push_proxmox_stats.py
```

You should see log lines with CPU/mem/disk/VM counts and a `200` from the
TRMNL push (this will fail until you complete step 4 below and have a
real `TRMNL_PLUGIN_UUID` in `.env`).

## 3. Schedule it

TRMNL's webhook rate limit is **12 payloads/hour on standard accounts**
(30/hour on TRMNL+), so don't go faster than every 5 minutes. Every 15
minutes is a comfortable default (4/hour) and plenty fresh for a homelab
dashboard.

**systemd timer (recommended):**

```bash
sudo cp systemd/trmnl-proxmox.service /etc/systemd/system/
sudo cp systemd/trmnl-proxmox.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trmnl-proxmox.timer
systemctl list-timers trmnl-proxmox.timer   # confirm it's scheduled
```

**Or plain cron**, if you'd rather not deal with systemd units:

```cron
*/15 * * * * cd /opt/trmnl-proxmox-plugin && ./venv/bin/python push_proxmox_stats.py >> /var/log/trmnl-proxmox.log 2>&1
```

## 4. Create the TRMNL private plugin

1. On the TRMNL dashboard, enable **Developer perks** (device picker →
   gear icon → Developer perks) if you haven't already.
2. **Plugins → add new → "Private Plugin"**, give it a name like
   "Proxmox".
3. Strategy: **Webhook**. Save the plugin — this generates its UUID,
   shown in the **Webhook URL** field
   (`https://trmnl.com/api/custom_plugins/<UUID>`). Copy the UUID into
   `.env` as `TRMNL_PLUGIN_UUID`.
4. Click **Edit Markup**. You'll see separate tabs for each layout size
   (Full, Half Horizontal, Half Vertical, Quadrant). Paste
   `templates/full.liquid` into the **Full** tab, and (optionally)
   `templates/half_horizontal.liquid` into the **Half Horizontal** tab if
   you want to pair it with another plugin on a split screen. You don't
   need to fill in every tab — just the sizes you plan to use.
5. Run the script once more (`./venv/bin/python push_proxmox_stats.py`)
   now that `.env` is complete, then use TRMNL's markup editor preview /
   "Force Refresh" on the plugin to check it renders correctly before
   adding it to a device playlist.
6. Add the plugin to your device's playlist (Device → Playlist → add
   plugin).

## Payload format

The script POSTs this shape as `merge_variables` (see
`push_proxmox_stats.py` for the exact fields):

```json
{
  "node": "epyc-pve",
  "cpu_percent": 12.4,
  "mem_used_gb": 48.2,
  "mem_total_gb": 251.0,
  "mem_percent": 19.2,
  "disk_used_gb": 820.0,
  "disk_total_gb": 2000.0,
  "disk_percent": 41.0,
  "uptime": "14d 6h",
  "vm_total": 12,
  "vm_running": 9,
  "updated_at": "Aug 17, 9:15 AM",
  "vms": [
    {"vmid": 101, "name": "vpn-tun", "type": "lxc", "status": "running"}
  ]
}
```

TRMNL webhook payloads are capped at **2KB** (standard) / **5KB**
(TRMNL+). The script measures the actual JSON size and trims the `vms`
list (stopped VMs first) until it fits, so it won't silently fail if you
have a lot of guests — you'll just see fewer of the least-relevant
(stopped) ones listed. Tune `MAX_VMS_LISTED` / `TRMNL_MAX_PAYLOAD_BYTES`
in `.env` if needed.

## Customizing the look

The templates use TRMNL's Framework CSS (`item`, `value`, `label`,
`columns`, `title_bar`, etc.) — see https://trmnl.com/framework/docs for
the full component reference if you want to reshape the layout, add a
Quadrant view, or swap in `value--peta` for a giant single stat.
