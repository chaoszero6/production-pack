"""Generate DOCX patent documents from the markdown specifications."""
import os
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

PATENT_DIR = os.path.dirname(os.path.abspath(__file__))


def set_style(doc):
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style.paragraph_format.space_after = Pt(6)
    style.paragraph_format.line_spacing = 1.5


def ap(doc, text, bold=False, center=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.font.name = "Times New Roman"
    r.font.size = Pt(12)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return p


def md_to_docx(md_path, docx_path, margins):
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    doc = Document()
    set_style(doc)
    for s in doc.sections:
        s.top_margin = Cm(margins[0])
        s.bottom_margin = Cm(margins[1])
        s.left_margin = Cm(margins[2])
        s.right_margin = Cm(margins[3])

    in_code = False
    for line in md.split("\n"):
        line = line.rstrip()

        # Toggle code blocks
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            if line.strip():
                p = doc.add_paragraph()
                r = p.add_run(line)
                r.font.name = "Courier New"
                r.font.size = Pt(10)
            continue

        # Headings
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("#### "):
            doc.add_heading(line[5:], level=4)
        elif line.startswith("---"):
            continue
        elif line.strip() == "":
            continue
        elif line.startswith("| "):
            # Table rows - render as plain text
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if cells and not all(c.startswith("-") for c in cells):
                ap(doc, "  |  ".join(cells))
        else:
            clean = line.replace("**", "").replace("*", "")
            if clean.strip():
                is_bold = line.startswith("**") or (line.startswith("- **") and "**" in line[4:])
                ap(doc, clean, bold=is_bold)

    doc.save(docx_path)
    print(f"Saved: {docx_path}")


if __name__ == "__main__":
    # Indian Patent (IPO margins: top 4cm, bottom 3cm, left 4cm, right 3cm)
    md_to_docx(
        os.path.join(PATENT_DIR, "PATENT_SPECIFICATION.md"),
        os.path.join(PATENT_DIR, "INDIA_Patent_Specification_Form2.docx"),
        margins=(4, 3, 4, 3),
    )

    # PCT International (WIPO margins: top 2.5cm, bottom 2cm, left 2.5cm, right 1.5cm)
    md_to_docx(
        os.path.join(PATENT_DIR, "PCT_INTERNATIONAL_SPECIFICATION.md"),
        os.path.join(PATENT_DIR, "PCT_International_Specification.docx"),
        margins=(2.5, 2, 2.5, 1.5),
    )

    print("\nDone! DOCX files ready for patent filing.")
