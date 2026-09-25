# YAMS App Store for Umbrel

A community app store for [umbrelOS](https://umbrel.com) with one app: **YAMS** ([Yet Another Media Server](https://yams.media)), packaged so a complete beginner can go from "install" to "watching" without touching a terminal or a config file.

One install gives you Jellyfin, Sonarr, Radarr, Prowlarr (+ FlareSolverr), Bazarr and qBittorrent locked behind a VPN, all already connected to each other, plus a setup dashboard that walks you through the four things only you can decide.

## Publish the store (one time, about 3 minutes)

1. Create a new **public** GitHub repository, for example `yams-umbrel-store`.
2. Upload everything in this folder to it (drag the files into GitHub's "Add file → Upload files" page, or `git push`). Keep the folder structure exactly as it is, including the empty `data/` folders.
3. Your store address is the repository URL. For this store it's:

   ```
   https://github.com/Deepshah0308/yams-umbrel-store
   ```

That's it. There's nothing to build; every image is pulled from Docker Hub.

## Install YAMS (what you tell a newbie)

1. In Umbrel, open the **App Store**, click the **⋯** menu (top right) and choose **Community App Stores**.
2. Paste `https://github.com/Deepshah0308/yams-umbrel-store` and click **Add**. Umbrel will warn that community apps aren't reviewed by Umbrel; that's expected.
3. Open **YAMS App Store**, click **YAMS**, then **Install**. The first install downloads roughly 2–3 GB of images, so give it a few minutes.
4. Open YAMS. The dashboard lights up four steps in order:
   - **Connect a VPN.** Download a WireGuard file from your VPN's website and paste it in (or choose the file). YAMS checks it, fixes the common problems, and connects. Downloads stay off until this works.
   - **Choose where YAMS searches.** Add your indexers in Prowlarr. They flow to Sonarr and Radarr automatically.
   - **Add a show or a movie** in Sonarr or Radarr.
   - **Start watching** in Jellyfin at `http://umbrel.local:8097`, using the login shown on the dashboard.

## What's done automatically

| Normally you'd have to… | YAMS on Umbrel does it for you |
|---|---|
| Run Jellyfin's setup wizard and create libraries | Creates the admin user and **Movies** + **Shows** libraries, with real-time folder watching |
| Copy API keys between apps | Generates keys up front and wires Sonarr, Radarr, Prowlarr and Bazarr together |
| Add qBittorrent to Sonarr and Radarr | Done, with `tv` / `movies` categories |
| Set library folders | `/data/media/tv` and `/data/media/movies`, hardlink-friendly ([TRaSH layout](https://trash-guides.info/File-and-Folder-Structure/)) |
| Set up FlareSolverr | Added to Prowlarr with the `flaresolverr` tag |
| Write gluetun environment variables | Paste a WireGuard file; the hostname is resolved to an IP and IPv6 is stripped, which gluetun requires |
| Turn on file renaming | On in both Sonarr and Radarr |
| Log in to five different web UIs | Everything except Jellyfin sits behind your Umbrel login |

The one thing left for you inside an app is picking subtitle languages in **Bazarr → Settings → Languages**.

## How it fits together

```
Umbrel login ──► web (Caddy) ─┬─ /            dashboard (setup + auto-wiring)
                              ├─ /sonarr      Sonarr
                              ├─ /radarr      Radarr
                              ├─ /prowlarr    Prowlarr ── FlareSolverr
                              ├─ /bazarr      Bazarr
                              └─ /qbittorrent qBittorrent ─┐
                                                           │ shares network with
port 8097 ──► Jellyfin                          gluetun (WireGuard VPN)
```

**Downloads can't leak.** qBittorrent runs inside gluetun's network, so its only route out is the VPN, and gluetun's firewall blocks everything else. On top of that, qBittorrent won't start until the VPN reports healthy, and a watchdog stops it within about 15 seconds if the VPN drops. When you switch VPN files, YAMS stops qBittorrent first, reconnects, then brings it back.

**Files** live in Umbrel's Downloads folder: `Downloads/torrents` (in progress) and `Downloads/media` (your library). Because both are on one mount, Sonarr and Radarr hardlink finished downloads instead of copying them, so seeding doesn't double your disk usage.

## Repository layout

```
umbrel-app-store.yml          store id "yams"
yams-media/
  umbrel-app.yml              listing shown in Umbrel
  docker-compose.yml          the stack
  app/
    setup/seed.py             first boot: keys, passwords, starter configs, folders
    dashboard/server.py       dashboard API + wires apps together via their APIs
    dashboard/index.html      the setup dashboard
    vpn/vpn-wrapper.sh        starts gluetun once a VPN file exists; reconnects on change
    qbittorrent/entry.sh      holds qBittorrent until the VPN is healthy
    qbittorrent/vpn-guard.sh  stops qBittorrent if the VPN drops
    Caddyfile                 routes every UI behind the Umbrel login
  data/                       persistent config (empty until first run)
```

## Troubleshooting

- **VPN says "Can't connect".** Download a file for a different server. Some providers' files expire, and a few (e.g. NordVPN) don't offer WireGuard downloads at all; Proton VPN, Mullvad and AirVPN all do.
- **Port 8097 is taken.** Change `8097:8096` in `docker-compose.yml` and `JELLYFIN_PORT` on the dashboard service to the same new number.
- **You changed the qBittorrent password.** Update it in Sonarr and Radarr under Settings → Download Clients.
- **Logs.** Umbrel → right-click YAMS → Troubleshoot shows every container's logs. Lines starting with `[yams]` come from this package.
- **Start over.** Uninstall YAMS (this deletes its settings, not your media in Downloads) and install again.

## Known limits

- VPN setup supports WireGuard files. OpenVPN-only providers aren't covered by the dashboard yet.
- VPN port forwarding isn't configured, so torrents work but may connect to fewer peers.
- No hardware transcoding by default. On Intel machines you can add `devices: ["/dev/dri:/dev/dri"]` to the Jellyfin service.
- Only the Downloads folder is used; external drives need Umbrel 2.0's folder access feature or a manual mount.

## Credits

[YAMS](https://yams.media) by rogs, [gluetun](https://github.com/qdm12/gluetun), the [linuxserver.io](https://linuxserver.io) images, [TRaSH Guides](https://trash-guides.info), and the [Umbrel community app store template](https://github.com/getumbrel/umbrel-community-app-store).
