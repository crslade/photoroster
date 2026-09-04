#!/usr/bin/env python3
"""Render docs/sample.png for the README.

Uses entirely fabricated names and flat colour tiles in place of photos, so
the repository never contains anyone's real roster data.

    python examples/make_sample.py
"""

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import photo_roster  # noqa: E402

NAMES = [
    "Amelia Hartwell", "Bo Li", "Carlos Mendoza-Reyes", "Dae-jung Park",
    "Eleanor Vance", "Faisal Al-Rashid", "Grace O'Connell", "Hiro Tanaka",
    "Imani Okafor", "Jonas Bergström", "Kavya Ramanathan", "Liam Doyle",
    "Maya Fitzgerald", "Nikolai Petrov", "Olive Brennan", "Priya Nair",
    "Quinn Alvarado", "Rosa Delacroix-Fontaine", "Samuel Ng", "Tevita Halapua",
    "Uma Krishnan", "Viktor Novak", "Wren Ashworth", "Ximena Castillo",
    "Yusuf Demir", "Zoe Lindqvist", "Aaron Whitfield", "Bianca Moreau",
    "Caleb Sørensen", "Delphine Aubert",
]

PALETTE = [
    (0.62, 0.68, 0.78), (0.72, 0.70, 0.66), (0.58, 0.70, 0.68),
    (0.76, 0.68, 0.70), (0.66, 0.64, 0.76), (0.70, 0.74, 0.64),
]


def tile(color, w=113, h=150):
    """A flat placeholder standing in for a student photo."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h))
    pix.set_rect(pix.irect, tuple(int(c * 255) for c in color))
    return pix.tobytes("png")


def main():
    root = Path(__file__).resolve().parent.parent
    out_png = root / "docs" / "sample.png"
    out_png.parent.mkdir(exist_ok=True)

    # A throwaway PDF holding the placeholder images, so build_pdf can pull
    # them out by xref exactly as it does from a real roster.
    holder = pymupdf.open()
    page = holder.new_page(width=300, height=2000)
    for i in range(len(NAMES)):
        page.insert_image(pymupdf.Rect(0, 0, 20, 26),
                          stream=tile(PALETTE[i % len(PALETTE)]))
    xrefs = [img[0] for img in page.get_images()]

    students = [{"name": n, "xref": xrefs[i % len(xrefs)]}
                for i, n in enumerate(NAMES)]
    students[7]["xref"] = None  # show the "no photo" placeholder

    tmp_pdf = root / "docs" / "sample.pdf"
    photo_roster.build_pdf(students, tmp_pdf, "CS 140-01", "Web Design",
                           5, 5, holder, "Sample output — fabricated names")

    doc = pymupdf.open(tmp_pdf)
    doc[0].get_pixmap(dpi=110).save(out_png)
    doc.close()
    tmp_pdf.unlink()
    print(f"wrote {out_png}")


if __name__ == "__main__":
    main()
