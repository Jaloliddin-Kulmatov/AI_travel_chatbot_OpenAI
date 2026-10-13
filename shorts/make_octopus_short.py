"""Render a 15-second 9:16 "fun fact" short (octopus hearts) with Pillow + ffmpeg.

Usage: PIPER_VOICE=/path/en_US-ryan-high.onnx python3 make_octopus_short.py [output.mp4]
Visuals are drawn in code, music/sfx are synthesized (ambient pad, heartbeat, pops).
If PIPER_VOICE points at a Piper voice model (pip install piper-tts), a narration
is synthesized offline and mixed over the music; without it the short is music-only.
"""
import math
import os
import random
import struct
import subprocess
import tempfile
import sys
import wave

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS, DUR = 1080, 1920, 30, 15.0
SR = 44100
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "octopus_hearts_short.mp4")
VOICE = os.environ.get("PIPER_VOICE")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PINK, RED, CYAN, BLUE, WHITE = (255, 120, 160), (240, 50, 80), (110, 225, 255), (70, 165, 255), (255, 255, 255)

# Timeline (seconds)
BEAT0, BEAT_PERIOD = 1.3, 0.8
T_GILLS, T_BODY, T_BLUE, T_CTA = 3.4, 6.8, 10.0, 13.0
HEART_END = 9.8

# (start time, narration text, piper length-scale: lower = faster)
NARRATION = [
    (0.5, "Did you know? An octopus has three hearts!", 0.9),
    (T_GILLS + 0.1, "Two pump blood to the gills.", 0.9),
    (T_BODY + 0.1, "One pumps blood to the whole body.", 0.9),
    (T_BLUE + 0.1, "And their blood is blue! Thanks to copper.", 0.9),
    (T_CTA - 0.05, "Follow for more mind-blowing facts.", 0.82),
]


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_back(t):
    t = clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


# ---------- sprites ----------
def font(size):
    return ImageFont.truetype(FONT, size)


def text_sprite(text, size, color=WHITE):
    f = font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), text, font=f, stroke_width=8)
    img = Image.new("RGBA", (r - l + 24, b - t + 24), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((12 - l, 12 - t), text, font=f, fill=color + (255,), stroke_width=8, stroke_fill=(10, 14, 40, 255))
    if img.width > 940:
        img = img.resize((940, int(img.height * 940 / img.width)), Image.LANCZOS)
    return img


def heart_sprite(size, color):
    ss = 4
    s = size * ss
    pts = []
    for i in range(400):
        t = i / 400 * 2 * math.pi
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((s / 2 + x / 17 * s * 0.48, s * 0.47 - y / 17 * s * 0.48))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=color + (255,))
    hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hi).ellipse((s * 0.2, s * 0.18, s * 0.42, s * 0.34), fill=(255, 255, 255, 90))
    img = Image.alpha_composite(img, hi)  # highlight
    return img.resize((size, size), Image.LANCZOS)


def drop_sprite(size, color):
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = s * 0.34
    cx, cy = s / 2, s * 0.64
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (255,))
    d.polygon([(cx, s * 0.06), (cx - r * 0.93, cy - r * 0.35), (cx + r * 0.93, cy - r * 0.35)], fill=color + (255,))
    hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hi).ellipse((s * 0.34, s * 0.5, s * 0.44, s * 0.68), fill=(255, 255, 255, 80))
    img = Image.alpha_composite(img, hi)
    img = img.resize((size, size), Image.LANCZOS)
    d2 = ImageDraw.Draw(img)
    f = font(int(size * 0.26))
    d2.text((size / 2, size * 0.66), "Cu", font=f, fill=(255, 255, 255, 255), anchor="mm")
    return img


def glow(sprite, color, radius=40):
    pad = radius * 3
    base = Image.new("RGBA", (sprite.width + pad * 2, sprite.height + pad * 2), (0, 0, 0, 0))
    mask = Image.new("L", base.size, 0)
    mask.paste(sprite.split()[3], (pad, pad))
    mask = mask.filter(ImageFilter.GaussianBlur(radius))
    base.paste(Image.new("RGBA", base.size, color + (255,)), (0, 0), mask.point(lambda v: int(v * 0.8)))
    return base


def with_alpha(sprite, a):
    if a >= 0.999:
        return sprite
    s = sprite.copy()
    s.putalpha(s.split()[3].point(lambda v: int(v * clamp(a))))
    return s


def blit(canvas, sprite, cx, cy, scale=1.0, alpha=1.0):
    if alpha <= 0.01 or scale <= 0.01:
        return
    if abs(scale - 1) > 0.005:
        sprite = sprite.resize((max(1, int(sprite.width * scale)), max(1, int(sprite.height * scale))), Image.LANCZOS)
    sprite = with_alpha(sprite, alpha)
    canvas.alpha_composite(sprite, (int(cx - sprite.width / 2), int(cy - sprite.height / 2)))


# ---------- background ----------
def make_bg():
    top, bot = (14, 22, 64), (8, 90, 120)
    strip = Image.new("RGB", (1, H))
    strip.putdata([tuple(int(top[i] + (bot[i] - top[i]) * y / H) for i in range(3)) for y in range(H)])
    return strip.resize((W, H)).convert("RGBA")


def make_bubbles(n=14):
    random.seed(7)
    out = []
    for _ in range(n):
        r = random.randint(30, 110)
        spr = Image.new("RGBA", (r * 2 + 40, r * 2 + 40), (0, 0, 0, 0))
        ImageDraw.Draw(spr).ellipse((20, 20, 20 + r * 2, 20 + r * 2), fill=(160, 230, 255, 38))
        out.append((spr.filter(ImageFilter.GaussianBlur(6)), random.uniform(0, W), random.uniform(0, H), random.uniform(25, 70)))
    return out


# ---------- audio ----------
def synth_audio(path):
    n = int(SR * DUR)
    buf = [0.0] * n

    def add(t0, dur, fn, gain):
        i0 = int(t0 * SR)
        for i in range(int(dur * SR)):
            if i0 + i >= n:
                break
            buf[i0 + i] += gain * fn(i / SR)

    # ambient pad: Am add9 w/ slow swell
    for f, g in [(110.0, 0.05), (164.81, 0.04), (220.0, 0.04), (246.94, 0.03), (329.63, 0.02)]:
        for i in range(n):
            t = i / SR
            env = clamp(t / 1.5) * clamp((DUR - t) / 1.0)
            buf[i] += g * env * math.sin(2 * math.pi * f * t) * (0.8 + 0.2 * math.sin(2 * math.pi * 0.2 * t + f))

    def thump(t):  # kick-like heartbeat thud
        f = 42 + 60 * math.exp(-t * 30)
        return math.sin(2 * math.pi * f * t) * math.exp(-t * 14)

    beat = BEAT0
    while beat < HEART_END:
        add(beat, 0.4, thump, 0.75)
        add(beat + 0.18, 0.4, thump, 0.5)
        beat += BEAT_PERIOD

    def pop(f):
        return lambda t: math.sin(2 * math.pi * f * (1 + 0.5 * math.exp(-t * 40)) * t) * math.exp(-t * 22)

    for t0, f in [(0.1, 880), (0.6, 740), (1.3, 988), (T_GILLS, 784), (T_BODY, 784), (T_BLUE, 660), (T_CTA, 988)]:
        add(t0, 0.3, pop(f), 0.22)

    def whoosh(t):
        return math.sin(2 * math.pi * (300 + 1800 * t) * t) * math.exp(-((t - 0.25) ** 2) / 0.02) * 0.5

    add(T_BLUE - 0.1, 0.6, whoosh, 0.25)

    mpeak = max(abs(v) for v in buf) or 1
    buf = [v / mpeak * 0.3 for v in buf]  # keep music under the voice

    if VOICE:
        with tempfile.TemporaryDirectory() as tmp:
            for k, (t0, text, ls) in enumerate(NARRATION):
                raw, clean = os.path.join(tmp, f"r{k}.wav"), os.path.join(tmp, f"c{k}.wav")
                subprocess.run([sys.executable, "-m", "piper", "-m", VOICE, "-f", raw, "--length-scale", str(ls)],
                               input=text.encode(), check=True, capture_output=True)
                trim = "silenceremove=start_periods=1:start_threshold=-50dB"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-af",
                                f"{trim},areverse,{trim},areverse,loudnorm=I=-14", "-ar", str(SR), "-ac", "1", clean], check=True)
                with wave.open(clean) as w:
                    pcm = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
                i0 = int(t0 * SR)
                for i, v in enumerate(pcm):
                    if i0 + i < n:
                        buf[i0 + i] += v / 32768 * 0.8

    peak = max(abs(v) for v in buf) or 1
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(clamp(v / max(peak, 1) * 0.9, -1, 1) * 32767)) for v in buf))


# ---------- scene ----------
def pulse(t):
    """0..1 bump following lub-dub heartbeat; 0 before first beat."""
    if t < BEAT0 or t > HEART_END + 0.3:
        return 0.0
    p = (t - BEAT0) % BEAT_PERIOD
    return max(math.exp(-((p) ** 2) / 0.004), 0.7 * math.exp(-((p - 0.18) ** 2) / 0.004))


def render():
    bg, bubbles = make_bg(), make_bubbles()
    heart_pink, heart_red = heart_sprite(330, PINK), heart_sprite(330, RED)
    glow_pink, glow_red = glow(heart_pink, PINK), glow(heart_red, RED)
    drop = drop_sprite(520, BLUE)
    drop_glow = glow(drop, BLUE, 50)

    T = {
        "dyk": text_sprite("DID YOU KNOW?", 64, CYAN),
        "has": text_sprite("An octopus has", 92),
        "three": text_sprite("3 HEARTS", 190, PINK),
        "two": text_sprite("2 pump blood", 100),
        "gills": text_sprite("to the gills", 100, PINK),
        "one": text_sprite("1 pumps blood", 100),
        "body": text_sprite("to the whole body", 100, RED),
        "their": text_sprite("And their blood", 96),
        "blue": text_sprite("is BLUE!", 170, BLUE),
        "cu": text_sprite("Thanks to copper, not iron", 62, CYAN),
        "follow": text_sprite("Follow for more", 110),
        "facts": text_sprite("mind-blowing facts", 100, CYAN),
    }

    audio = os.path.join(os.path.dirname(OUT) or ".", "_audio.wav")
    synth_audio(audio)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", audio, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
         "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-shortest", "-movflags", "+faststart", OUT],
        stdin=subprocess.PIPE,
    )

    def pop_in(canvas, key, t, t0, cx, cy, t_out=None, scale=1.0):
        if t < t0:
            return
        s = ease_back((t - t0) / 0.35)
        a = clamp((t - t0) / 0.2)
        if t_out is not None and t > t_out:
            a *= 1 - clamp((t - t_out) / 0.25)
        blit(canvas, T[key], cx, cy, s * scale, a)

    HY = 760  # hearts row y
    positions = {"L": (190, HY + 40), "C": (W // 2, HY - 20), "R": (W - 190, HY + 40)}
    heart_t0 = {"L": BEAT0 + 0.0, "C": BEAT0 + 0.35, "R": BEAT0 + 0.7}

    for fi in range(int(DUR * FPS)):
        t = fi / FPS
        c = bg.copy()
        # drifting bubbles
        for spr, x, y, sp in bubbles:
            blit(c, spr, (x + 30 * math.sin(t + x)) % W, (y - sp * t) % (H + 200) - 100)

        # hearts
        fade_h = 1 - clamp((t - HEART_END) / 0.4)
        for k, (cx, cy) in positions.items():
            if t < heart_t0[k] or fade_h <= 0:
                continue
            gill = k != "C"
            base_scale = 0.86 if gill else 1.12
            appear = ease_back((t - heart_t0[k]) / 0.4)
            hl = 0.35 if (T_GILLS <= t < T_BODY and not gill) or (T_BODY <= t < T_BLUE and gill) else 1.0
            emph = 1.0
            if (T_GILLS <= t < T_BODY and gill) or (T_BODY <= t < T_BLUE and not gill):
                emph = 1.12
            sc = base_scale * appear * emph * (1 + 0.13 * pulse(t))
            blit(c, glow_pink if gill else glow_red, cx, cy, sc, 0.55 * hl * fade_h)
            blit(c, heart_pink if gill else heart_red, cx, cy, sc, hl * fade_h)

        # text blocks
        TY = 1250
        pop_in(c, "dyk", t, 0.1, W // 2, 450, t_out=T_GILLS - 0.2)
        pop_in(c, "has", t, 0.6, W // 2, TY - 150, t_out=T_GILLS - 0.2)
        pop_in(c, "three", t, 1.3, W // 2, TY + 30, t_out=T_GILLS - 0.2)
        pop_in(c, "two", t, T_GILLS, W // 2, TY - 60, t_out=T_BODY - 0.2)
        pop_in(c, "gills", t, T_GILLS + 0.25, W // 2, TY + 70, t_out=T_BODY - 0.2)
        pop_in(c, "one", t, T_BODY, W // 2, TY - 60, t_out=HEART_END)
        pop_in(c, "body", t, T_BODY + 0.25, W // 2, TY + 70, t_out=HEART_END)

        # blue blood scene
        if T_BLUE <= t < T_CTA + 0.1:
            fade = 1 - clamp((t - (T_CTA - 0.2)) / 0.3)
            dp = ease_back((t - T_BLUE) / 0.5)
            wob = 1 + 0.04 * math.sin((t - T_BLUE) * 5)
            blit(c, drop_glow, W // 2, 700, dp * wob, 0.6 * fade)
            blit(c, drop, W // 2, 700, dp * wob, fade)
            pop_in(c, "their", t, T_BLUE + 0.15, W // 2, 1170, t_out=T_CTA - 0.2)
            pop_in(c, "blue", t, T_BLUE + 0.45, W // 2, 1350, t_out=T_CTA - 0.2)
            pop_in(c, "cu", t, T_BLUE + 1.0, W // 2, 1510, t_out=T_CTA - 0.2)
    
        # CTA
        if t >= T_CTA:
            ring = (t - T_CTA) * 1.2
            d = ImageDraw.Draw(c)
            for off in (0, 0.5):
                p = (ring + off) % 1
                r = 120 + 360 * p
                d.ellipse((W // 2 - r, 900 - r, W // 2 + r, 900 + r), outline=CYAN + (int(110 * (1 - p)),), width=6)
            pop_in(c, "follow", t, T_CTA, W // 2, 880)
            pop_in(c, "facts", t, T_CTA + 0.2, W // 2, 1020)

        # progress bar
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((60, 70, W - 60, 82), radius=6, fill=(255, 255, 255, 50))
        d.rounded_rectangle((60, 70, 60 + (W - 120) * t / DUR, 82), radius=6, fill=CYAN + (255,))

        # fade in/out
        a = min(clamp(t / 0.25), clamp((DUR - t) / 0.3))
        if a < 1:
            c = Image.blend(Image.new("RGBA", (W, H), (8, 12, 40, 255)), c, a)
        ff.stdin.write(c.convert("RGB").tobytes())

    ff.stdin.close()
    ff.wait()
    os.remove(audio)
    print("wrote", OUT)


if __name__ == "__main__":
    render()
