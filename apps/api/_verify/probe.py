"""Build real documents and push them through DoclingEngine exactly as ingest does."""
import io, time, json
import fitz, docx
from opencortyx.ingest.formats.unstructured.docling.engine import DoclingEngine

def make_pdf():
    d = fitz.open(); p = d.new_page()
    y = 72
    def w(t, s=11, bold=False):
        nonlocal y
        p.insert_text((72, y), t, fontsize=s, fontname="helv" if not bold else "hebo"); y += s * 1.8
    w("Master Services Agreement", 20, True)
    w("1. Payment Terms", 15, True)
    w("Northwind Supplies shall be paid 4,000 GBP per month in arrears.")
    w("1.1 Late payment", 12, True)
    w("Interest accrues at 2% per month on overdue invoices.")
    w("2. Termination", 15, True)
    w("Either party may terminate with 90 days written notice.")
    y += 10
    rows = [["Vendor", "Amount", "Currency"], ["Northwind", "4000", "GBP"], ["Acme", "1200", "USD"]]
    for r in rows:
        for j, c in enumerate(r):
            p.insert_text((72 + j * 120, y), c, fontsize=11)
            p.draw_rect(fitz.Rect(68 + j * 120, y - 13, 188 + j * 120, y + 5))
        y += 18
    return d.tobytes()

def make_docx():
    doc = docx.Document()
    doc.add_heading("Onboarding Guide", 0)
    doc.add_heading("Week one", 1)
    doc.add_paragraph("Meet your manager and set up your laptop.")
    doc.add_heading("Accounts", 2)
    doc.add_paragraph("Request access to email and the wiki.", style="List Bullet")
    doc.add_heading("Week two", 1)
    doc.add_paragraph("Shadow a senior engineer.")
    t = doc.add_table(rows=2, cols=2); t.cell(0,0).text="Tool"; t.cell(0,1).text="Owner"; t.cell(1,0).text="Jira"; t.cell(1,1).text="Ops"
    b = io.BytesIO(); doc.save(b); return b.getvalue()

MD = b"# Handbook\n\nIntro para.\n\n## Leave\n\nYou get 25 days.\n\n### Sick leave\n\nTen days paid.\n\n## Expenses\n\n| Item | Limit |\n|---|---|\n| Hotel | 150 |\n\n- receipts required\n"

def show(name, content, fname):
    t = time.time()
    a = DoclingEngine.convert(content, filename=fname, doc_id="doc")
    used = "docling" if DoclingEngine._converter_instance is not None else "FALLBACK"
    print(f"\n===== {name}  ({time.time()-t:.1f}s, engine={used}) =====")
    byid = {n.id: n for n in a.nodes}
    for n in a.nodes:
        par = byid[n.parent_id].text[:22] if n.parent_id in byid else n.parent_id
        print(f"  {n.type:<14} lvl={str(n.level):<4} p{n.metadata.page_number} parent={par!s:<24} | {n.text[:50]!r}"
              + (f" grid={n.grid}" if n.grid else ""))
    print("  stats:", a.summary_stats())

show("PDF", make_pdf(), "msa.pdf")
show("DOCX", make_docx(), "onboarding.docx")
show("MARKDOWN", MD, "handbook.md")
show("TXT", b"Plain text line one.\nLine two.", "notes.txt")
