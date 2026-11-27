<!-- Is this a new layout or a layout fix? Send it to https://github.com/brianmuse/art-frames-layouts instead. -->

## What this changes

<!-- One change per pull request. Link the issue for a big change. -->

## Why

## How you tested it

<!-- Name the TV model and firmware if you checked by hand on a real TV. -->

## Checklist

- [ ] `uv run ruff check .` and `uv run pytest` pass.
- [ ] No test contacts a real TV, presence sensor or NAS.
- [ ] A new setting is in the schema, `config.example.toml`, `docs/configuration.md` and the form.
- [ ] I changed a dependency: `uv.lock` and the three `requirements*.lock` exports are updated. (Delete this line if not.)
- [ ] Docs and UI text are in plain language and say "layout", not "app".
- [ ] No IP addresses, MAC addresses, tokens, passwords, calendar links, real names or household screenshots.
