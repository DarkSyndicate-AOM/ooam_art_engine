"""
VenDrop — FLUX Image Renderer
Reads image_prompt fields from output/designs.json, generates high-resolution
wall art via FLUX1.1 Pro on fal.ai, saves images locally, then uploads them
to Printify and patches each product's print_area with the real image ID.

Why FLUX1.1 Pro via fal.ai:
  - Best quality-to-cost ratio for automated pipelines in 2026
  - ~$0.04–0.06 per image at 3000×4000px print resolution
  - No subscription — pure pay-per-use
  - Apache 2.0 commercial license: images are yours to sell
  - Simple Python SDK, async support, built-in retry

Setup:
  pip install fal-client requests python-dotenv pillow

Env vars (.env):
  FAL_KEY=your_fal_api_key           # fal.ai → Settings → API Keys
  PRINTIFY_API_TOKEN=your_token
  PRINTIFY_SHOP_ID=your_shop_id

Usage:
  python image_renderer.py                    # Render all pending designs
  python image_renderer.py --limit 5          # Render first 5 only
  python image_renderer.py --design-id my_id # Render one specific design
  python image_renderer.py --dry-run          # Preview prompts, no API calls
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

FAL_KEY              = os.getenv("FAL_KEY")
PRINTIFY_API_TOKEN   = os.getenv("PRINTIFY_API_TOKEN")
PRINTIFY_SHOP_ID     = os.getenv("PRINTIFY_SHOP_ID")

OUTPUT_DIR   = Path("output")
IMAGES_DIR   = OUTPUT_DIR / "images"
DESIGNS_FILE = OUTPUT_DIR / "designs.json"
LOG_FILE     = OUTPUT_DIR / "renderer.log"

# FLUX1.1 Pro on fal.ai — best quality for print production
FAL_MODEL = "fal-ai/flux-pro/v1.1"

# Print-ready resolution: 3000×4000px at 150–200 DPI for 16×20" canvas
IMAGE_WIDTH  = 3000
IMAGE_HEIGHT = 4000

# Cost guard: stop if estimated spend exceeds this per run
MAX_SPEND_PER_RUN_USD = 5.00
COST_PER_IMAGE_USD    = 0.05   # ~$0.04–0.06 at this resolution

# Seconds between API calls (fal.ai rate limit courtesy)
API_DELAY = 2

# ── Logging ───────────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("renderer")

# ── fal.ai client (lazy import so script still loads without fal installed) ──

def _get_fal():
    try:
        import fal_client
        return fal_client
    except ImportError:
        raise SystemExit("Run: pip install fal-client")


# ── Printify helpers ──────────────────────────────────────────────────────────

PRINTIFY_HEADERS = {
    "Authorization": f"Bearer {PRINTIFY_API_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "VenDrop/1.0",
}
PRINTIFY_BASE = "https://api.printify.com/v1"


def printify_get(path: str) -> dict:
    r = requests.get(f"{PRINTIFY_BASE}{path}", headers=PRINTIFY_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def printify_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{PRINTIFY_BASE}{path}", headers=PRINTIFY_HEADERS,
                      json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def printify_put(path: str, payload: dict) -> dict:
    r = requests.put(f"{PRINTIFY_BASE}{path}", headers=PRINTIFY_HEADERS,
                     json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_shop_id() -> str:
    if PRINTIFY_SHOP_ID:
        return PRINTIFY_SHOP_ID
    shops = printify_get("/shops.json")
    return str(shops[0]["id"])


def upload_image_to_printify(image_path: Path, filename: str, shop_id: str) -> str:
    """
    Upload a local image file to Printify's media library.
    Returns the Printify image ID to use in print_areas.
    """
    import base64
    data = base64.b64encode(image_path.read_bytes()).decode()
    payload = {
        "file_name": filename,
        "contents":  data,
    }
    result = printify_post(f"/shops/{shop_id}/uploads/images.json", payload)
    image_id = result["id"]
    log.info(f"    ↑ Uploaded to Printify: {filename} → {image_id}")
    return image_id


def patch_product_image(printify_id: str, image_id: str, shop_id: str):
    """Update the product's print area with the real image ID."""
    product = printify_get(f"/shops/{shop_id}/products/{printify_id}.json")
    variants = product.get("variants", [])
    variant_ids = [v["id"] for v in variants if v.get("is_enabled")]

    updated_print_areas = [
        {
            "variant_ids": variant_ids,
            "placeholders": [
                {
                    "position": "front",
                    "images": [
                        {
                            "id":    image_id,
                            "x":     0.5,
                            "y":     0.5,
                            "scale": 1.0,
                            "angle": 0,
                        }
                    ],
                }
            ],
        }
    ]
    printify_put(
        f"/shops/{shop_id}/products/{printify_id}.json",
        {"print_areas": updated_print_areas},
    )
    log.info(f"    ✓ Patched Printify product {printify_id} with image {image_id}")


# ── FLUX generation ───────────────────────────────────────────────────────────

def enhance_prompt_for_print(raw_prompt: str, style: str, palette: list[str]) -> str:
    """
    Wrap the AI-generated image prompt with print-production boilerplate.
    This consistently improves output for wall art:
      - High resolution / print quality keywords
      - Correct aspect ratio framing
      - Removes photographic artifacts
    """
    style_suffix = {
        "canvas art":          "fine art canvas print, gallery quality, rich color depth",
        "framed poster":       "premium poster art, professional photography, crisp detail",
        "metal print":         "dramatic contrast, metallic sheen quality, vivid saturation",
        "black & white print": "monochrome fine art, deep blacks, luminous highlights",
    }.get(style.lower(), "premium wall art print")

    palette_hint = ""
    if palette:
        palette_hint = f"dominant colors: {', '.join(palette[:3])}. "

    return (
        f"{raw_prompt}. "
        f"{palette_hint}"
        f"{style_suffix}. "
        "Ultra high resolution, 300 DPI print quality, no watermarks, "
        "no text overlays, no borders, clean composition suitable for "
        "framing. Aspect ratio 3:4 portrait orientation."
    )


def generate_image_flux(prompt: str, design_id: str) -> Path | None:
    """
    Call FLUX1.1 Pro on fal.ai and save the result locally.
    Returns the local file path, or None on failure.
    """
    fal = _get_fal()

    os.environ["FAL_KEY"] = FAL_KEY or ""

    output_path = IMAGES_DIR / f"{design_id}.png"
    if output_path.exists():
        log.info(f"  ⏭  Image already exists: {output_path.name}")
        return output_path

    log.info(f"  🎨 Generating: {design_id}")
    log.info(f"     Prompt: {prompt[:120]}...")

    try:
        result = fal.subscribe(
            FAL_MODEL,
            arguments={
                "prompt":       prompt,
                "image_size": {
                    "width":  IMAGE_WIDTH,
                    "height": IMAGE_HEIGHT,
                },
                "num_inference_steps": 28,     # quality vs speed balance
                "guidance_scale":       3.5,   # FLUX default — don't over-tune
                "num_images":           1,
                "output_format":       "png",  # lossless for print production
                "enable_safety_checker": True,
            },
            with_logs=False,
        )

        images = result.get("images", [])
        if not images:
            log.error(f"  ✗ No images returned for {design_id}")
            return None

        image_url = images[0]["url"]
        log.info(f"     Generated URL: {image_url[:80]}...")

        # Download the image locally
        r = requests.get(image_url, timeout=60)
        r.raise_for_status()
        output_path.write_bytes(r.content)
        size_kb = output_path.stat().st_size // 1024
        log.info(f"  ✓ Saved: {output_path.name} ({size_kb} KB)")
        return output_path

    except Exception as e:
        log.error(f"  ✗ FLUX generation failed for {design_id}: {e}")
        return None


# ── Main render loop ──────────────────────────────────────────────────────────

def render_pending(limit: int | None = None,
                   target_id: str | None = None,
                   dry_run: bool = False):

    if not DESIGNS_FILE.exists():
        log.error("No designs.json found. Run generate_designs.py first.")
        return

    library  = json.loads(DESIGNS_FILE.read_text())
    shop_id  = get_shop_id() if PRINTIFY_API_TOKEN else None

    # Filter to designs that need images
    if target_id:
        queue = [d for d in library if d["id"] == target_id]
    else:
        queue = [
            d for d in library
            if d.get("status") in ("pending", "listed")
            and not (IMAGES_DIR / f"{d['id']}.png").exists()
        ]

    if limit:
        queue = queue[:limit]

    if not queue:
        log.info("No designs need image generation.")
        return

    estimated_cost = len(queue) * COST_PER_IMAGE_USD
    log.info(f"Queue: {len(queue)} designs | Estimated cost: ${estimated_cost:.2f}")

    if estimated_cost > MAX_SPEND_PER_RUN_USD:
        log.warning(
            f"Estimated cost ${estimated_cost:.2f} exceeds cap "
            f"${MAX_SPEND_PER_RUN_USD:.2f}. Pass --limit N to reduce batch size."
        )
        return

    if dry_run:
        for d in queue:
            print(f"\n── {d['id']} ──")
            print(f"Style:  {d.get('style')}")
            print(f"Prompt: {d.get('image_prompt', '')[:200]}")
        return

    rendered = 0
    for design in queue:
        design_id = design["id"]
        raw_prompt = design.get("image_prompt", "")
        style      = design.get("style", "canvas art")
        palette    = design.get("color_palette", [])

        if not raw_prompt:
            log.warning(f"  ⚠  No image_prompt for {design_id} — skipping")
            continue

        # Build production-quality prompt
        full_prompt = enhance_prompt_for_print(raw_prompt, style, palette)

        # Generate via FLUX
        image_path = generate_image_flux(full_prompt, design_id)

        if image_path and shop_id:
            # Upload to Printify
            printify_image_id = upload_image_to_printify(
                image_path,
                filename=f"{design_id}.png",
                shop_id=shop_id,
            )
            design["printify_image_id"] = printify_image_id

            # Patch the Printify product if it's already been created
            if design.get("printify_id") and design["printify_id"] != "placeholder":
                try:
                    patch_product_image(
                        design["printify_id"], printify_image_id, shop_id
                    )
                    design["status"] = "image_ready"
                except Exception as e:
                    log.error(f"  ✗ Could not patch product: {e}")

        rendered += 1
        # Save progress after each image in case of interruption
        DESIGNS_FILE.write_text(json.dumps(library, indent=2))

        time.sleep(API_DELAY)

    log.info(f"\nDone. Rendered {rendered}/{len(queue)} images.")
    log.info(f"Actual cost estimate: ${rendered * COST_PER_IMAGE_USD:.2f}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VenDrop FLUX Image Renderer")
    parser.add_argument("--limit",     type=int,   help="Max images to render this run")
    parser.add_argument("--design-id", type=str,   help="Render a single design by ID")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print prompts without calling fal.ai")
    args = parser.parse_args()

    render_pending(
        limit=args.limit,
        target_id=args.design_id,
        dry_run=args.dry_run,
    )
