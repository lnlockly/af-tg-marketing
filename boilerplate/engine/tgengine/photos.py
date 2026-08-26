"""
photos.py — resolve a POOL of local photo files to set on an account.

DIVISION OF LABOR (important): this engine only APPLIES photos it is handed. It does
NOT search the web and does NOT generate images. The AGENT sources the photos with its
OWN native tools — `image_generate` for synthetic faces, an image-search / browser tool
for web pictures — and passes the resulting FILE PATHS here (account_dress --photos).
Real accounts have SEVERAL photos, so dressing sets a gallery.

SAFETY (for the agent, enforced in the `dressing` skill): use the user's own photos,
synthetic/AI faces, or generic stock — NEVER the photos of a specific real, identifiable
person (impersonation).
"""
from __future__ import annotations

import os

_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".img")
MAX_PHOTOS = 6


def collect_local(spec: str | None) -> list[str]:
    """Expand a --photos spec into existing image file paths.

    `spec` is either a DIRECTORY (all images inside, sorted) or a comma-separated list
    of file paths. Returns only files that exist. This is the ONLY photo source the
    engine knows about — the agent has already fetched/generated them.
    """
    if not spec:
        return []
    out: list[str] = []
    if os.path.isdir(spec):
        for name in sorted(os.listdir(spec)):
            if name.lower().endswith(_IMG_EXT):
                out.append(os.path.join(spec, name))
    else:
        for part in spec.split(","):
            p = part.strip()
            if p and os.path.isfile(p):
                out.append(p)
    # dedup preserve order, cap
    seen, deduped = set(), []
    for p in out:
        if p not in seen:
            seen.add(p); deduped.append(p)
    return deduped[:MAX_PHOTOS]
