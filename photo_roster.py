#!/usr/bin/env python3
"""Build a compact photo-roster PDF (photo + name grid) from a Workday
"View Course Section Roster" PDF export.

Everything runs locally: the source PDF is read, photos and names are pulled
out in memory, and a new PDF is written next to it. Nothing is uploaded.

Usage:
    python photo_roster.py CS_140-01_-_WEB_DESIGN.pdf
    python photo_roster.py roster.pdf -o out.pdf --course "CS 140-01" \
        --title "Web Design" --cols 5 --rows 5 --sort

Copyright (c) 2026 Christopher Slade. Licensed CC BY 4.0; see LICENSE.
"""

import argparse
import re
import sys
from pathlib import Path

import pymupdf


# ---------------------------------------------------------------- extraction

# Column bands (PDF points) on the Workday roster page. The photo column and
# the "Student" name column are found by their header text, so these are only
# fallbacks if the headers are missing.
FALLBACK_PHOTO_X = (250.0, 286.0)
FALLBACK_NAME_X = (285.0, 365.0)

# Vertical gap (pt) that separates two students rather than two wrapped lines.
ROW_GAP = 20.0


def find_columns(page):
    """Locate the Photo and Student columns from the table header row."""
    photo = name = email = None
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                x0, _, x1, _ = span["bbox"]
                if text == "Photo":
                    photo = (x0, x1)
                elif text == "Student":
                    name = (x0, x1)
                elif text == "Email Address":
                    email = (x0, x1)
    if not name:
        return FALLBACK_PHOTO_X, FALLBACK_NAME_X
    # Header labels are centred over their column; widen to the neighbours.
    left = photo[1] + 4 if photo else name[0] - 25
    right = email[0] - 4 if email else name[1] + 55
    photo_band = (photo[0] - 40, photo[1] + 40) if photo else FALLBACK_PHOTO_X
    return photo_band, (left, right)


HEADER_LABELS = {
    "Student", "Photo", "Email Address", "Credits", "Academic Level",
    "Academic Unit", "Program of Study", "Registration", "Registration Status",
    "Student Course Registration", "Status",
}


def name_rows(page, name_x, header_bottom):
    """Names in the Student column, top to bottom.

    The export draws each table row as one text block spanning several
    columns, so rows are rebuilt from individual spans: keep the spans whose
    x falls inside the Student column, then group them by vertical gap (lines
    of one wrapped name sit ~9pt apart; separate students ~38pt apart).
    """
    lo, hi = name_x
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"]
                     if s["bbox"][0] >= lo and s["bbox"][2] <= hi
                     and s["bbox"][1] >= header_bottom]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            if text:
                lines.append((spans[0]["bbox"][1], text))

    lines.sort(key=lambda t: t[0])
    rows, cur, top = [], [], None
    for y, text in lines:
        if top is not None and y - top > ROW_GAP:
            rows.append({"name": " ".join(cur), "top": top})
            cur = []
        if not cur:
            top = y
        cur.append(text)
    if cur:
        rows.append({"name": " ".join(cur), "top": top})

    out = []
    for r in rows:
        name = re.sub(r"\s+", " ", r["name"]).strip()
        if name and name not in HEADER_LABELS:
            out.append({"name": name, "top": r["top"]})
    return out


def photos_on_page(page, photo_x):
    """Images in the photo column, top to bottom, with their xref."""
    lo, hi = photo_x
    out = []
    for info in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        cx = (x0 + x1) / 2
        if not (lo <= cx <= hi):
            continue
        if info["xref"] == 0 or (x1 - x0) < 8 or (y1 - y0) < 8:
            continue
        out.append({"xref": info["xref"], "bbox": (x0, y0, x1, y1)})
    out.sort(key=lambda p: p["bbox"][1])
    return out


def extract_students(doc):
    """Pair each Student-column name with the photo on its row."""
    students = []
    for page in doc:
        photo_x, name_x = find_columns(page)
        header_bottom = 0.0
        for label in ("Student Course Registration", "Registered Students"):
            hits = page.search_for(label)
            if hits:
                header_bottom = max(header_bottom, max(r.y1 for r in hits) + 2)
        rows = name_rows(page, name_x, header_bottom)
        photos = photos_on_page(page, photo_x)

        # A row band runs from just above a name's first line to just above
        # the next name's first line; the photo whose centre lands inside is
        # that student's.
        for i, row in enumerate(rows):
            top = row["top"] - 8
            bottom = rows[i + 1]["top"] - 8 if i + 1 < len(rows) else 1e6
            xref = None
            for p in photos:
                cy = (p["bbox"][1] + p["bbox"][3]) / 2
                if top <= cy < bottom:
                    xref = p["xref"]
                    break
            students.append({"name": row["name"], "xref": xref})
    return students


def course_info(doc):
    """Course number and title from the report heading, if present."""
    head = doc[0].get_text("text", clip=pymupdf.Rect(0, 0, doc[0].rect.x1, 90))
    head = re.sub(r"\s+", " ", head).strip()
    m = re.search(r"Roster:\s*(.+?)\s+-\s+(.+?)"
                  r"(?:\s+\d{1,2}:\d{2}\s*[AP]M|\s+Page \d|$)", head)
    if m:
        return m.group(1).strip(), m.group(2).strip().title()
    return "", ""


# ------------------------------------------------------------------- output

MAC_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


def needs_unicode_font(students):
    """Base-14 Helvetica covers Latin-1; anything beyond needs a real font."""
    for stu in students:
        try:
            stu["name"].encode("latin-1")
        except UnicodeEncodeError:
            return True
    return False


def pick_font(page, explicit=None):
    """Embed a Unicode-capable font so accented names render correctly."""
    candidates = [explicit] if explicit else MAC_FONTS
    for path in candidates:
        if path and Path(path).exists():
            try:
                page.insert_font(fontname="roster", fontfile=path)
                return "roster", path
            except Exception:
                continue
    return "helv", None


def fit_lines(text, font, size, width, max_lines=2):
    """Greedy word wrap; returns None if it will not fit in max_lines."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if font.text_length(trial, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    if any(font.text_length(l, size) > width for l in lines):
        return None
    return lines


def build_pdf(students, out_path, course, title, cols, rows, src_doc, subtitle=""):
    page_w, page_h = pymupdf.paper_size("letter")
    margin = 36
    per_page = cols * rows
    total_pages = max(1, -(-len(students) // per_page))

    out = pymupdf.open()
    embed = needs_unicode_font(students)
    font_obj = None
    fontname = "helv"

    for pno in range(total_pages):
        page = out.new_page(width=page_w, height=page_h)
        if embed:
            fontname, fontfile = pick_font(page)
            if font_obj is None:
                font_obj = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font("helv")
        elif font_obj is None:
            font_obj = pymupdf.Font("helv")

        # Heading
        head_y = margin + 14
        # "\u00b7" (not an em dash) so it renders under base-14 Helvetica too.
        heading = " \u00b7 ".join(p for p in (course, title) if p) or "Class Roster"
        page.insert_text((margin, head_y), heading, fontname=fontname,
                         fontsize=16, color=(0, 0, 0))
        sub = subtitle or f"{len(students)} students"
        page.insert_text((margin, head_y + 15), sub, fontname=fontname,
                         fontsize=9, color=(0.4, 0.4, 0.4))
        if total_pages > 1:
            label = f"Page {pno + 1} of {total_pages}"
            page.insert_text((page_w - margin - font_obj.text_length(label, 9), head_y),
                             label, fontname=fontname, fontsize=9, color=(0.4, 0.4, 0.4))
        page.draw_line(pymupdf.Point(margin, head_y + 24),
                       pymupdf.Point(page_w - margin, head_y + 24),
                       color=(0.75, 0.75, 0.75), width=0.6)

        grid_top = head_y + 36
        grid_w = page_w - 2 * margin
        grid_h = page_h - margin - grid_top
        cell_w = grid_w / cols
        cell_h = grid_h / rows
        name_h = 24
        pad = 4

        chunk = students[pno * per_page:(pno + 1) * per_page]
        for idx, stu in enumerate(chunk):
            r, c = divmod(idx, cols)
            cx0 = margin + c * cell_w
            cy0 = grid_top + r * cell_h

            # Photo box: 3:4 portrait, centred in the cell's upper area.
            box_h = cell_h - name_h - 2 * pad
            box_w = min(cell_w - 2 * pad, box_h * 0.75)
            box_h = min(box_h, box_w / 0.75)
            px0 = cx0 + (cell_w - box_w) / 2
            rect = pymupdf.Rect(px0, cy0 + pad, px0 + box_w, cy0 + pad + box_h)

            if stu["xref"]:
                try:
                    img = src_doc.extract_image(stu["xref"])
                    page.insert_image(rect, stream=img["image"], keep_proportion=True)
                except Exception:
                    stu["xref"] = None
            if not stu["xref"]:
                page.draw_rect(rect, color=(0.8, 0.8, 0.8), fill=(0.95, 0.95, 0.95),
                               width=0.5)
                note = "no photo"
                page.insert_text(
                    (rect.x0 + (box_w - font_obj.text_length(note, 7)) / 2,
                     rect.y0 + box_h / 2),
                    note, fontname=fontname, fontsize=7, color=(0.6, 0.6, 0.6))

            # Name, shrinking until it fits two lines.
            avail = cell_w - 2
            for size in (8.5, 8, 7.5, 7, 6.5, 6):
                lines = fit_lines(stu["name"], font_obj, size, avail)
                if lines:
                    break
            else:
                size = 6
                lines = [stu["name"]]
                while font_obj.text_length(lines[0], size) > avail and len(lines[0]) > 4:
                    lines[0] = lines[0][:-2] + "…"

            ty = rect.y1 + 9
            for line in lines:
                tx = cx0 + (cell_w - font_obj.text_length(line, size)) / 2
                page.insert_text((tx, ty), line, fontname=fontname, fontsize=size)
                ty += size + 1.5

    if embed:
        try:  # keeps a large Unicode font from bloating the file
            out.subset_fonts()
        except Exception:
            pass
    out.save(out_path, garbage=3, deflate=True)
    out.close()
    return total_pages


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="Workday class roster PDF")
    ap.add_argument("-o", "--output", help="output PDF (default: <input>_photo_roster.pdf)")
    ap.add_argument("--course", help='course number, e.g. "CS 140-01"')
    ap.add_argument("--title", help='course title, e.g. "Web Design"')
    ap.add_argument("--subtitle", default="", help="line under the heading")
    ap.add_argument("--cols", type=int, default=5, help="photos across (default 5)")
    ap.add_argument("--rows", type=int, default=5, help="photos down (default 5)")
    ap.add_argument("--sort", action="store_true", help="sort alphabetically by name")
    args = ap.parse_args()

    src = Path(args.pdf)
    if not src.exists():
        sys.exit(f"No such file: {src}")

    doc = pymupdf.open(src)
    students = extract_students(doc)
    if not students:
        sys.exit("Found no students — is this a Workday course section roster export?")
    if args.sort:
        students.sort(key=lambda s: s["name"].lower())

    course, title = course_info(doc)
    course = args.course or course
    title = args.title or title

    out_path = Path(args.output) if args.output else src.with_name(
        src.stem + "_photo_roster.pdf")
    pages = build_pdf(students, out_path, course, title, args.cols, args.rows,
                      doc, args.subtitle)
    missing = sum(1 for s in students if not s["xref"])
    doc.close()

    print(f"{len(students)} students ({missing} without a photo) "
          f"-> {out_path} [{pages} page(s)]")


if __name__ == "__main__":
    main()
