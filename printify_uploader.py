"""
VenDrop — Printify Auto-Uploader
Reads designs from output/designs.json and creates products in Printify
via their REST API, then marks designs as 'listed'.

Setup:
  pip install requests python-dotenv

Env vars needed (.env):
  PRINTIFY_API_TOKEN=your_token_here
  PRINTIFY_SHOP_ID=your_shop_id_here

Usage:
  python printify_uploader.py             # Upload all pending designs
  python printify_uploader.py --dry-run   # Preview without uploading
"""

import os
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

API_TOKEN   = os.getenv("PRINTIFY_API_TOKEN")
SHOP_ID     = os.getenv("PRINTIFY_SHOP_ID")
BASE_URL    = "https://api.printify.com/v1"
DESIGNS_FILE = Path("output/designs.json")

# Printify Blueprint IDs for wall art
# These are Printify's internal product IDs — update if needed
BLUEPRINT_MAP = {
    "canvas art":          {"blueprint_id": 1209, "print_provider_id": 99},  # Sensaria canvas
    "framed poster":       {"blueprint_id": 1188, "print_provider_id": 99},  # Sensaria framed
    "metal print":         {"blueprint_id": 1146, "print_provider_id": 99},  # Metal print
    "black & white print": {"blueprint_id": 1188, "print_provider_id": 99},  # Framed poster B&W
}

# Standard size variant map (width x height in inches → Printify variant label)
SIZE_VARIANT_LABELS = {
    "12x16": '12" x 16"',
    "16x20": '16" x 20"',
    "24x32": '24" x 32"',
}

LOG_FILE = Path("output/uploader.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("printify-uploader")

# ── API helpers ───────────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "VenDrop/1.0",
}


def api_get(path: str) -> dict:
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS,
                      json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_shop_id() -> str:
    if SHOP_ID:
        return SHOP_ID
    shops = api_get("/shops.json")
    return str(shops[0]["id"])


def get_blueprint_variants(blueprint_id: int, provider_id: int) -> list[dict]:
    """Fetch available variants for a blueprint + provider combo."""
    data = api_get(f"/catalog/blueprints/{blueprint_id}"
                   f"/print_providers/{provider_id}/variants.json")
    return data.get("variants", [])


# ── Product builder ───────────────────────────────────────────────────────────

def build_product_payload(design: dict, shop_id: str) -> dict:
    style   = design.get("style", "canvas art").lower()
    mapping = BLUEPRINT_MAP.get(style, BLUEPRINT_MAP["canvas art"])
    blueprint_id  = mapping["blueprint_id"]
    provider_id   = mapping["print_provider_id"]

    # Fetch variants to get correct IDs
    all_variants = get_blueprint_variants(blueprint_id, provider_id)

    # Match requested sizes to Printify variant IDs
    requested_sizes = design.get("size_options", ["16x20"])
    selected_variants = []
    for size in requested_sizes:
        label = SIZE_VARIANT_LABELS.get(size, size)
        for v in all_variants:
            if label in v.get("title", ""):
                selected_variants.append({
                    "id":      v["id"],
                    "price":   int(design.get("price", 49.99) * 100),  # cents
                    "is_enabled": True,
                })
                break

    if not selected_variants:
        # Fall back: enable first 3 variants
        selected_variants = [
            {"id": v["id"], "price": int(design.get("price", 49.99) * 100),
             "is_enabled": True}
            for v in all_variants[:3]
        ]

    # Build bullet point description
    bullets = "\n".join(f"• {b}" for b in design.get("bullet_points", []))
    full_desc = f"{design.get('description', '')}\n\n{bullets}"

    payload = {
        "title":       design["title"],
        "description": full_desc,
        "blueprint_id": blueprint_id,
        "print_provider_id": provider_id,
        "variants": selected_variants,
        "print_areas": [
            {
                "variant_ids": [v["id"] for v in selected_variants],
                "placeholders": [
                    {
                        "position": "front",
                        "images": [
                            {
                                # Placeholder — replace with real uploaded image ID
                                # Use printify_uploader.upload_image() after generating art
                                "id":    "placeholder",
                                "x":     0.5,
                                "y":     0.5,
                                "scale": 1,
                                "angle": 0,
                            }
                        ],
                    }
                ],
            }
        ],
        "tags": design.get("amazon_keywords", [])[:13],  # Printify max 13
    }
    return payload


def upload_image_from_url(image_url: str, filename: str) -> str:
    """Upload an image URL to Printify and return the image ID."""
    shop_id = get_shop_id()
    payload = {"file_name": filename, "url": image_url}
    result  = api_post(f"/shops/{shop_id}/uploads/images.json", payload)
    return result["id"]


# ── Main uploader ─────────────────────────────────────────────────────────────

def upload_pending(dry_run: bool = False):
    if not DESIGNS_FILE.exists():
        log.error("No designs.json found. Run generate_designs.py first.")
        return

    library  = json.loads(DESIGNS_FILE.read_text())
    pending  = [d for d in library if d.get("status") == "pending"]
    shop_id  = get_shop_id()
    log.info(f"Shop ID: {shop_id} | Pending designs: {len(pending)}")

    uploaded = 0
    for design in pending:
        try:
            payload = build_product_payload(design, shop_id)

            if dry_run:
                log.info(f"[DRY RUN] Would create: {design['title']}")
                continue

            result = api_post(f"/shops/{shop_id}/products.json", payload)
            design["status"]       = "listed"
            design["printify_id"]  = result.get("id")
            design["listed_at"]    = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            uploaded += 1
            log.info(f"  ✓ Created: {design['title']} → {result.get('id')}")
            time.sleep(1)  # rate limit courtesy

        except Exception as e:
            log.error(f"  ✗ Failed: {design.get('title', 'unknown')} — {e}")

    # Save updated statuses
    DESIGNS_FILE.write_text(json.dumps(library, indent=2))
    log.info(f"Done. Uploaded {uploaded}/{len(pending)} designs.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Printify Product Uploader")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making API calls")
    args = parser.parse_args()
    upload_pending(dry_run=args.dry_run)
