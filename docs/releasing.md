# Releasing

A release is a version tag such as `v1.2.3`. Pushing the tag starts `.github/workflows/release.yml`. Nothing else starts it. The workflow:

1. Checks that the tag matches `__version__` in `art_frames/__init__.py`.
2. Runs ruff, pytest and `docker compose config` on the install files.
3. Builds the four images on native amd64 and arm64 runners: `art-frames`, `art-frames-renderer`, `art-frames-renderer-static` and `art-frames-website`, all under `ghcr.io/brianmuse`.
4. Joins each image into one multi-platform tag, `1.2.3`.
5. Moves the short tags only to the highest release: `1.2` when this is the highest `1.2.x` tag, `1` when it is the highest `1.x.y` tag, and `latest` when it is the highest tag of all. A fix for an older line, for example `v1.1.5` after `v1.2.0`, gets `1.1.5` and `1.1` only. A prerelease such as `v1.3.0-rc.1` gets only its own tag.
6. Creates the GitHub Release with notes made from the merged pull requests. A prerelease tag makes a prerelease. The in-app update check reads only the latest full release.

## Before the first release

Do these once, in this order. Until step 3 is done, nobody can pull the images without signing in to GHCR.

1. **Rename the repository** to `art-frames` (**Settings → General → Repository name**). The update check asks `repos/brianmuse/art-frames/releases/latest`, and the image labels name that repository. GitHub sends the old URL on to the new one, but update local remotes: `git remote set-url origin https://github.com/brianmuse/art-frames.git`.
2. **Make the repository public** (**Settings → General → Danger Zone → Change visibility**). First check that no private value is in its history.
3. **Check the action pins.** In `release.yml`, two actions are pinned to a commit with only a `# v3` comment: `docker/setup-buildx-action@8d2750c…` and `docker/login-action@c94ce9f…`. Open each repository's v3 tag on GitHub and check that the commit is the same. Pin to the exact release (for example `# v3.10.0`) if you can.
4. **Push a first tag** (below). The workflow creates the four packages.
5. **Make the four packages public.** On GitHub, open **Your profile → Packages**, then each of `art-frames`, `art-frames-renderer`, `art-frames-renderer-static` and `art-frames-website`. Open **Package settings → Change visibility → Public**. Under **Manage Actions access**, check that the repository has write access.
6. On a machine that is not signed in to GHCR, run `docker pull ghcr.io/brianmuse/art-frames:<version>`. It must work.
7. **Release the layouts too.** The install file also runs `ghcr.io/brianmuse/art-frames-layout-morning`, `-news` and `-weather`. The layouts repository (`art-frames-layouts`) builds them from its own version tag, with its own `release.yml`. Make that repository public, push its first tag, and make its three packages public in the same way. Until then, `docker compose up` fails to pull the layout images.

## Each release

1. Change `__version__` in `art_frames/__init__.py` to the new version, for example `1.2.3`. Commit it on `main`.
2. Make sure the checks on `main` pass.
3. Tag and push:

   ```sh
   git tag v1.2.3
   git push origin v1.2.3
   ```

4. Watch **Actions → Release**. When it ends, check the release page and the package tags.
5. Install from `install/docker-compose.yml` on a test machine, and open Art Frames: the foot of the sidebar must name the new version. **Layouts** must list Morning dashboard, News & weather and Weather.

If a job fails, fix the cause on `main`, change the version (for example to `1.2.4`) and tag again. Do not move a tag that people may already have pulled.
