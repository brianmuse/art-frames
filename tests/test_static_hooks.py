"""Every data-* hook a template uses has its handler in app.js."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / "art_frames"
NOT_FOR_APP_JS = {"data-dayframe-ready"}  # the renderer waits for it


def test_every_template_hook_is_handled_by_app_js():
    script = (ROOT / "static" / "app.js").read_text()
    hooks = set()
    for template in (ROOT / "templates").glob("*.html"):
        hooks |= set(re.findall(r"\b(data-[a-z][a-z0-9-]*)", template.read_text()))

    def handled(hook):
        name = re.sub(r"-([a-z0-9])", lambda match: match.group(1).upper(), hook[5:])
        return hook in script or name in script

    assert hooks - NOT_FOR_APP_JS
    assert sorted(hook for hook in hooks - NOT_FOR_APP_JS if not handled(hook)) == []
