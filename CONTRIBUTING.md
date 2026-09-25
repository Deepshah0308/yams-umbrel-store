# Contributing to YAMS for Umbrel

Thanks for helping. Whether it's a bug report, a clearer sentence on the dashboard or a new feature, contributions are welcome. You don't need to be an expert: plenty of useful changes are a few lines.

## Ways to help

- **Report a problem.** [Open an issue](https://github.com/Deepshah0308/yams-umbrel-store/issues/new/choose) with the YAMS version and the logs from the dashboard's **Logs** button.
- **Suggest an improvement.** The best ideas start with "I keep having to…".
- **Improve the words.** The dashboard is written for people who've never run a media server. If something confused you, it will confuse others.
- **Fix or build something.** Look for issues labelled `good first issue`, or comment on one to say you're taking it.

## How the project fits together

```
umbrel-app-store.yml          store id "yams"
yams-media/
  umbrel-app.yml              the App Store listing, version and home-screen widgets
  docker-compose.yml          every container
  hooks/yams/                 all of YAMS's own code (see "Rules" for why it lives here)
    setup/seed.py             first boot: keys, passwords, starter configs, folders
    dashboard/server.py       dashboard API, app wiring, quick fixes, widgets, Docker controls
    dashboard/updater.py      "Update apps": registry checks, image pulls, container recreation
    dashboard/backups.py      backup, restore and the backup schedule
    dashboard/index.html      the dashboard page
    vpn/vpn-wrapper.sh        starts gluetun once a VPN file exists
    qbittorrent/*.sh          the VPN kill switch for qBittorrent
    Caddyfile                 routes each app's web UI behind the Umbrel login
docs/                         the GitHub Pages site
tests/                        automated tests (run on every pull request)
```

## Run the tests

You need Python 3.12 or newer.

```sh
pip install pytest pyyaml
pytest -q tests
```

The tests use fake versions of Docker, Sonarr, Radarr and Prowlarr, so they run anywhere in about ten seconds, with no Umbrel needed. Every pull request runs them automatically.

## Try your change on your own Umbrel

1. **Fork** this repository on GitHub and push your change to your fork.
2. In `yams-media/umbrel-app.yml`, raise `version` (for example `1.3.0` → `1.3.1-test`), and set `YAMS_VERSION` in `docker-compose.yml` to the same value.
3. In Umbrel, go to **App Store → ⋯ → Community App Stores**. Remove this store and add your fork's URL instead. Both use the store id `yams`, so only one can be added at a time.
4. YAMS shows an **Update** button (or install it fresh). The dashboard header shows your test version once it's running.
5. When you're done, switch back to `https://github.com/Deepshah0308/yams-umbrel-store`.

Umbrel re-copies `hooks/yams/` on every update, so code changes reach your Umbrel whenever you bump the version.

## Rules that keep YAMS safe and working

- **Keep code in `hooks/yams/`.** umbrelOS only re-copies certain files when an app updates, and the `hooks` folder is one of them. Code elsewhere would reach new installs but never existing ones. Never name a file after a hook (`pre-start`, `post-update`, …) because umbrelOS would run it.
- **Standard library only.** The dashboard runs on the stock `python:alpine` image, so there's nothing to `pip install`. This keeps installs fast and nothing to build.
- **qBittorrent only ever uses the VPN.** It shares gluetun's network, waits for the tunnel before starting, and stops if the tunnel goes. Any change near this needs a test showing downloads can't leave outside the VPN.
- **Only touch YAMS's own containers.** Anything using the Docker socket filters by the `com.docker.compose.project=yams-media` label.
- **Check what an app accepted.** When wiring apps together, start from the app's own schema (`…/schema` endpoints), then confirm the change worked before marking a step done. Otherwise failures go silent.
- **Write for newcomers.** Messages say what happened and what to do next, in plain words. "Can't reach qBittorrent right now. YAMS checks again every 10 minutes." beats "Connection refused (111)".
- **Add a test** for any behaviour you change. The fakes in `tests/fakes.py` make most things easy to test.

## Releasing a version

Maintainers release by merging to `main` with the version bumped in both `yams-media/umbrel-app.yml` and `YAMS_VERSION` in `yams-media/docker-compose.yml` (a test checks they match), plus a line in `CHANGELOG.md`. Umbrel then offers the update to everyone.

## Be kind

Treat everyone with respect. Assume good intent, explain rather than dismiss, and remember many people here are running a server for the first time. Harassment or personal attacks aren't welcome and will be removed. If something bothers you, open an issue or contact the maintainer.

## License

Contributions are shared under the same license as this repository (see `LICENSE`).
