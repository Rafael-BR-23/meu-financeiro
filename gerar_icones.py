import os
from PIL import Image, ImageDraw, ImageFont

def render_icon(size, output_path):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded rectangle
    bg_color = (12, 14, 18, 255) # #0c0e12
    corner_radius = int(size * 0.22)
    draw.rounded_rectangle([0, 0, size, size], radius=corner_radius, fill=bg_color)
    
    # Border
    border_color = (251, 191, 36, 120)
    draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=corner_radius, outline=border_color, width=max(2, size // 100))

    # Center wallet / card box
    box_x0 = int(size * 0.22)
    box_y0 = int(size * 0.32)
    box_x1 = int(size * 0.78)
    box_y1 = int(size * 0.70)
    box_radius = int(size * 0.07)
    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=box_radius, fill=(30, 36, 48, 255), outline=(251, 191, 36, 255), width=max(3, size // 50))

    # Dividing line in wallet
    line_y = int(size * 0.44)
    draw.line([box_x0, line_y, box_x1, line_y], fill=(255, 255, 255, 40), width=max(1, size // 120))

    # Coin / Clasp
    clasp_cx = int(size * 0.65)
    clasp_cy = int(size * 0.52)
    clasp_r = int(size * 0.08)
    draw.ellipse([clasp_cx - clasp_r, clasp_cy - clasp_r, clasp_cx + clasp_r, clasp_cy + clasp_r], fill=(217, 119, 6, 255), outline=(251, 191, 36, 255), width=max(2, size // 100))

    # Trend line (Emerald)
    points = [
        (int(size * 0.30), int(size * 0.60)),
        (int(size * 0.42), int(size * 0.49)),
        (int(size * 0.50), int(size * 0.55)),
        (int(size * 0.60), int(size * 0.43))
    ]
    draw.line(points, fill=(52, 211, 153, 255), width=max(3, size // 60), joint="curve")

    img.save(output_path, "PNG")
    print(f"Generated {output_path} ({size}x{size})")

os.makedirs("static", exist_ok=True)
render_icon(192, "static/icon-192.png")
render_icon(512, "static/icon-512.png")
render_icon(512, "static/icon-512-maskable.png")
