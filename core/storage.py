"""Where generated files go: always a local copy under invoices/, and Cloudinary when CLOUDINARY_URL is set,
so ASI:One (which can't reach this machine) gets a link it can open.

The link is a signed download link through Cloudinary's API, not the public delivery URL: free accounts
block public delivery of PDFs (401) unless a security setting is changed, and the signed link works either
way. It carries a signature, never the API secret, and stays valid for LINK_YEARS.
"""

import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from core import config

LOCAL_DIR = config.ROOT / "invoices"
LINK_YEARS = 10


@dataclass
class Stored:
    path: str                       # the local copy
    url: str | None = None          # download link, when uploaded


def safe_name(name: str) -> str:
    """"ARTH/26-27/0001" -> "ARTH-26-27-0001": a file name and a Cloudinary public id."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")


def _cloudinary():
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils

    u = urlparse(config.CLOUDINARY_URL)
    cloudinary.config(cloud_name=u.hostname, api_key=u.username, api_secret=u.password, secure=True)
    return cloudinary


def save_pdf(data: bytes, folder: str, name: str) -> Stored:
    """Write the PDF locally and, if Cloudinary is configured, upload it (replacing an earlier upload)."""
    folder_dir = LOCAL_DIR / safe_name(folder)              # invoice numbers restart per business
    folder_dir.mkdir(parents=True, exist_ok=True)
    path = folder_dir / f"{safe_name(name)}.pdf"
    path.write_bytes(data)
    if not config.CLOUDINARY_URL:
        return Stored(str(path))
    cloudinary = _cloudinary()
    public_id = f"arth/{safe_name(folder)}/{safe_name(name)}"
    cloudinary.uploader.upload(str(path), public_id=public_id, resource_type="image", overwrite=True, invalidate=True)
    link = cloudinary.utils.private_download_url(public_id, "pdf", resource_type="image", type="upload",
                                                 expires_at=int(time.time()) + LINK_YEARS * 365 * 24 * 3600)
    return Stored(str(path), link)
