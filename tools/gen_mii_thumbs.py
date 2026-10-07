"""Generates the Mii style pictures used by the editor's pickers.

Renders every valid Wii Mii style (hair, eyes, eyebrows, nose, mouth, face
shape, facial feature, glasses, mustache, beard) once with the bundled FFL
renderer in headless Chromium, crops each to the relevant part of the face
and packs them into one sprite sheet per category:
    mii_renderer/thumbs/<category>.png  +  mii_renderer/thumbs/thumbs.json

Pictures are baked at build time so the launcher does no rendering work (and
no GPU work) just to show them. Run from the repo root:
    pip install playwright pillow && python tools/gen_mii_thumbs.py
"""
import asyncio, base64, io, json, os, subprocess, sys, tempfile, time
from playwright.async_api import async_playwright
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "mii_renderer", "thumbs")

GEN_HTML = """<!doctype html><meta charset=utf-8><body>
<script src="mii_renderer/fflModule.cjs"></script>
<script type="module">
const {Mii}=await import("./mii_renderer/miijs.browser.esm.js");
const res=new Uint8Array(await (await fetch("RFL_Res.dat")).arrayBuffer());
const bytes=new Uint8Array(await (await fetch("starter.mii")).arrayBuffer());
let q=Promise.resolve();
window.shot=(fields,size,full)=>{const p=q.then(async()=>{const m=await Mii.create(bytes);
 for(const [k,v] of Object.entries(fields)) m.set(k,v);
 const png=await m.render(!!full,{size,fflResBuffer:res});let s="";const a=new Uint8Array(png);
 for(let i=0;i<a.length;i+=0x8000)s+=String.fromCharCode.apply(null,a.subarray(i,i+0x8000));return btoa(s);});
 q=p.catch(()=>{});return p;};
window.ready=true;
</script>"""

# Neutral base the pictures are rendered on (canonical values == Wii indices
# in the valid ranges, verified by sweeping the library's own encoder).
BASE = {"gender": 0, "faceType": 0, "faceColor": 0, "hairType": 33, "hairColor": 1,
        "eyeType": 0, "eyebrowType": 0, "eyebrowColor": 1, "noseType": 1, "mouthType": 1,
        "glassesType": 0, "mustacheType": 0, "beardType": 0, "faceFeature": 0, "makeup": 0}
BALD = 30  # hair style 30 is bald: shows the face part clearly

FEATURE = {0: [0, 0], 1: [0, 1], 2: [0, 3], 3: [0, 9], 4: [5, 0], 5: [2, 0],
           6: [1, 0], 7: [6, 0], 8: [8, 0], 9: [0, 10], 10: [9, 0], 11: [11, 0]}

# category: (field(s) per index, count, crop box in the 512px render, cell size, bald?)
def cats():
    return {
        "hair":     (lambda i: {"hairType": i}, 72, (56, 20, 456, 420), (128, 128), False),
        "face":     (lambda i: {"faceType": i, "hairType": BALD}, 8, (100, 60, 420, 440), (128, 128), True),
        "feature":  (lambda i: {"faceFeature": FEATURE[i][0], "makeup": FEATURE[i][1], "hairType": BALD}, 12, (100, 60, 420, 440), (128, 128), True),
        "eyes":     (lambda i: {"eyeType": i, "hairType": BALD}, 48, (135, 200, 385, 305), (160, 64), True),
        "brows":    (lambda i: {"eyebrowType": i, "hairType": BALD}, 24, (135, 175, 385, 270), (160, 61), True),
        "nose":     (lambda i: {"noseType": i, "hairType": BALD}, 12, (195, 298, 315, 388), (112, 112), True),
        "mouth":    (lambda i: {"mouthType": i, "hairType": BALD}, 24, (170, 320, 340, 405), (160, 80), True),
        "glasses":  (lambda i: {"glassesType": i, "hairType": BALD}, 9, (110, 190, 410, 320), (160, 69), True),
        "mustache": (lambda i: {"mustacheType": i, "hairType": BALD}, 4, (160, 290, 350, 410), (128, 82), True),
        "beard":    (lambda i: {"beardType": i, "hairType": BALD}, 4, (160, 290, 350, 430), (128, 94), True),
    }

def pick(png, box):
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return im.crop(box)

def avg(png, x, y, r=6):
    im = Image.open(io.BytesIO(png)).convert("RGB")
    px = [im.getpixel((x + dx, y + dy)) for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
    return "#%02x%02x%02x" % tuple(int(sum(p[k] for p in px) / len(px)) for k in range(3))

def most_saturated(png, box):
    """Colour of the most saturated area in a crop (the lips)."""
    import colorsys
    im = Image.open(io.BytesIO(png)).convert("RGB").crop(box)
    best, bc = -1, (0, 0, 0)
    w, h = im.size
    for yy in range(0, h, 2):
        for xx in range(0, w, 2):
            c = im.getpixel((xx, yy))
            _, sat, val = colorsys.rgb_to_hsv(*(v / 255 for v in c))
            score = sat * (val > 0.35)
            if score > best: best, bc = score, c
    return "#%02x%02x%02x" % bc


async def main():
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp()
    # serve the repo root plus the generator page
    with open(os.path.join(tmp, "gen.html"), "w") as f: f.write(GEN_HTML)
    for name in ("mii_renderer", "starter.mii", "RFL_Res.dat"):
        os.symlink(os.path.join(ROOT, name), os.path.join(tmp, name))
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8790", "-d", tmp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    meta = {}
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                              "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
            pg = await b.new_page()
            pg.on("pageerror", lambda e: print("PAGEERR", str(e)[:200]))
            await pg.goto("http://127.0.0.1:8790/gen.html")
            await pg.wait_for_function("window.ready===true", timeout=60000)

            async def shot(fields, size=512, full=False):
                return base64.b64decode(await pg.evaluate("([f,s,u])=>shot(f,s,u)", [{**BASE, **fields}, size, full]))

            for cat, (fn, count, box, cell, _bald) in cats().items():
                cols = 8 if cell[0] < 140 else 6
                rows = -(-count // cols)
                sheet = Image.new("RGBA", (cols * cell[0], rows * cell[1]), (0, 0, 0, 0))
                for i in range(count):
                    crop = pick(await shot(fn(i)), box)
                    # fit into the cell keeping aspect
                    crop.thumbnail(cell, Image.LANCZOS)
                    ox = (cell[0] - crop.width) // 2; oy = (cell[1] - crop.height) // 2
                    sheet.alpha_composite(crop, ((i % cols) * cell[0] + ox, (i // cols) * cell[1] + oy))
                path = os.path.join(OUT, cat + ".png")
                sheet.save(path, optimize=True)
                meta[cat] = {"file": cat + ".png", "cell": list(cell), "cols": cols, "count": count}
                print(f"{cat}: {count} pictures, {os.path.getsize(path)//1024} KB", flush=True)

            # swatch colours sampled from real renders (so they match the game's colours)
            LIP = {0: 0, 1: 15, 2: 21}
            sw = {"lip": [most_saturated(await shot({"mouthColor": LIP[i], "hairType": BALD, "mouthType": 0}), (200, 320, 320, 400)) for i in range(3)]}
            fav = []
            for i in range(12):
                fav.append(avg(await shot({"favoriteColor": i}, 512, True), 256, 300, 5))
            sw["fav"] = fav
            meta["_swatches"] = sw
            await b.close()
    finally:
        srv.terminate()
    with open(os.path.join(OUT, "thumbs.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print("done ->", OUT)

asyncio.run(main())
