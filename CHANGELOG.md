# Changelog

## 1.3.0
- **Security hardening.** The media apps moved to a private network other Umbrel apps can't reach. The dashboard's controls are only served to the router (behind your Umbrel login), and every command must come from the dashboard page, blocking cross-site requests. Security headers on every page.
- **Change password** updates Jellyfin, qBittorrent, Sonarr and Radarr together. The password is hidden on the dashboard until you press **Show**.
- A secret scan in the test suite fails the build if a password, VPN key or API key is ever committed.
- **Update apps yourself.** The dashboard checks Docker Hub for newer versions and updates every app with one click, or automatically each week. It backs up your settings first and rolls back any app that won't start.
- **Quick fixes:** connect Prowlarr or qBittorrent to Sonarr and Radarr, fix library folders, rescan Jellyfin, search for missing episodes and movies, reconnect the VPN, or fix everything at once.
- Prowlarr is now linked to Radarr as well as Sonarr, using each app's own form, and the links are re-checked every 10 minutes.
- GitHub Pages install guide, contributing guide, automated tests and CI.

## 1.2.2
- Sonarr and Radarr reliably get qBittorrent as their download client, re-checked and repaired every 10 minutes.

## 1.2.1
- Fixed qBittorrent failing to start ("unable to exec /app/qbittorrent-nox").

## 1.2.0
- Backups, like `yams backup`: on demand or automatic, with one-click restore.

## 1.1.0
- Logs, restart and VPN check for every app; update check; download list; home-screen widgets.
- qBittorrent waits for the VPN tunnel itself rather than gluetun's health check.

## 1.0.1
- New app icon. qBittorrent is pulled from Docker Hub.

## 1.0.0
- First release.
