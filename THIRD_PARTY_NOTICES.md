# Attribution

Art Frames is inspired by:

- [Frame Daily Plan](https://github.com/JustifyTechExpenses/frame-daily-plan): the morning planner idea and calendar parsing approach. TV uploads use the separately distributed Samsung SDK directly.
- [Docent](https://github.com/danmunz/docent): the curated gallery, collection, and framing workflow. No Docent application code is included.
- [samsung-tv-ws-api](https://github.com/xchwarze/samsung-tv-ws-api), distributed separately as the `samsungtvws` dependency under LGPL-3.0. Art Frames does not modify or statically link it.

The bundled Inter fonts are licensed under the SIL Open Font License. Their full license is in `art_frames/fonts/Inter-LICENSE.txt`. Copies served by the UI have the same license.

The renderer seccomp profile is adapted from [Microsoft Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json), derived from Docker's default profile. Art Frames adds an unconditional `chroot` syscall allowance so Chromium can construct its sandbox inside a user namespace while all container capabilities remain dropped. Its Apache-2.0 license is included in `renderer/PLAYWRIGHT_LICENSE`.

Museum imports retain their source URLs and public-domain metadata. Individual artwork is not covered by Art Frames' software license. User uploads and imported museum images remain runtime data.

The project website bundles [three.js](https://github.com/mrdoob/three.js) (MIT, Copyright 2010-2026 Three.js Authors) in `site/vendor/hero-room.js`. The license notice is kept at the end of that file.

The optional HTML/CSS renderer uses separately distributed [WeasyPrint](https://github.com/Kozea/WeasyPrint) (BSD-3-Clause) and [pypdfium2/PDFium](https://github.com/pypdfium2-team/pypdfium2). Their installed package and bundled binary license notices apply and remain in the renderer image. Art Frames does not modify or statically link those libraries. The optional website service uses Astro and its official Node adapter, both distributed under MIT.

## Frame Daily Plan license

MIT License

Copyright (c) 2026 JustifyTechExpenses

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
