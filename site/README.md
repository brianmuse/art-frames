# Project website

This folder is the Art Frames website for GitHub Pages: one static page, `index.html`. It has no tracking and no external scripts. It loads two fonts (Newsreader and DM Sans) from Google Fonts and falls back to system fonts.

`images` is a link to `../docs/images`, so the README and the website use the same image files. The Pages workflow packs the files with `tar --dereference`, so the link becomes real files on Pages.

## 3D bedroom in the hero

With WebGL, the hero is a 3D bedroom with a Frame TV and a wall clock. Scrolling moves the camera in toward the TV and runs a morning: the TV is off at 6:45 AM, turns on at 7:00 with the Morning layout, and fades to just the art at 9:00. Last, the camera moves down to a room sensor on the credenza. The wall clock shows the time, and sun from the window moves across the wall. Once the camera is close, small callouts point at the calendar, the art, and the sensor. On phones, the room draws fewer pixels on a shorter stage and ends on just the TV and the clock. Browsers without WebGL and readers who prefer reduced motion get the flat hero.

The TV screenshots have a 1280 px copy (`*-1280.webp`) for phones, and the sensor diagram uses `tv-artwork-mat-480.webp`.

- `hero-room/room.js` builds the room in [three.js](https://threejs.org/) (MIT). It uses no model, texture or HDRI files.
- `vendor/hero-room.js` is the built, minified file the page loads after it has painted. It is committed, so Pages needs no build step.

To change the room, edit `hero-room/room.js`, then rebuild and commit `vendor/hero-room.js`:

```sh
cd site/hero-room
npm install
npm run build
```

## Preview

Open `index.html` in a browser, or serve the folder:

```sh
python3 -m http.server --directory site 8000
```

## Publish

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) publishes this folder when a push to `main` changes `site/` or `docs/images/`. You can also start it by hand from the Actions tab.

GitHub Pages does not work while the repository is private on a free plan. When the repository is public:

1. Open **Settings → Pages**.
2. Set **Source** to **GitHub Actions**.
3. Run the **Pages** workflow once, or push a change to `site/`.
