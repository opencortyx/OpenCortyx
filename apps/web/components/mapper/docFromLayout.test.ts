/**
 * A PDF or Word file previewed in the Mapper must read the way ingestion reads it: the title, the
 * sections nested by level, each element under its own section with its own kind, and the running
 * page headers and footers kept apart from the sections they happen to sit next to.
 */
import { expect, test } from "@jest/globals";
import { docFromLayout, normalizeChunk } from "./model";

const el = (id: string, label: string, text: string, parent_id: string | null, level: number | null = null, page = 1) =>
  ({ id, label, text, level, page, parent_id });

test("a layout reading becomes sections with their own elements and the page furniture apart", () => {
  const doc = docFromLayout("handbook.pdf", [
    el("h1", "page_header", "ACME Corp - Employee Handbook", "doc"),
    el("t", "title", "Employee Handbook", "doc", 0),
    el("p0", "text", "This handbook explains onboarding.", "t"),
    el("s1", "section_header", "1. Onboarding", "t", 1),
    el("s11", "section_header", "1.1 First week", "s1", 2),
    el("li", "list_item", "Collect your laptop.", "s11"),
    el("f1", "page_footer", "Confidential - Page 1", "doc"),
    el("s2", "section_header", "2. Benefits", "t", 1, 2),
    el("tb", "table", "Benefit | Share", "s2", null, 2),
  ]).document;

  expect(doc.title).toBe("Employee Handbook");
  expect(doc.pages).toBe(2);
  expect(doc.sections.map((s) => [s.heading, s.level])).toEqual([
    ["Employee Handbook", 0], ["1. Onboarding", 1], ["1.1 First week", 2], ["2. Benefits", 1]]);
  expect(doc.sections[0].blocks.map((b) => b.kind)).toEqual(["text"]);
  expect(doc.sections[2].blocks.map((b) => [b.kind, b.text])).toEqual([["list_item", "Collect your laptop."]]);
  expect(doc.sections[3].blocks.map((b) => b.kind)).toEqual(["table"]);
  expect(doc.page_furniture.map((b) => [b.kind, b.page])).toEqual([["page_header", 1], ["page_footer", 1]]);
});

test("text before the first heading is kept, not dropped", () => {
  const doc = docFromLayout("memo.docx", [el("p", "text", "To all staff.", "doc"), el("h", "section_header", "Notice", "doc", 1)]).document;
  expect(doc.sections[0].blocks[0].text).toBe("To all staff.");
  expect(doc.title).toBe("memo.docx");
});

test("a mapping with no saved strategy reads documents by layout, as ingestion does", () => {
  expect(normalizeChunk({}).strategy).toBe("layout");
  expect(normalizeChunk({ strategy: "sliding" }).strategy).toBe("sliding");
  expect(normalizeChunk({ strategy: "layout_docling" }).strategy).toBe("layout");
});
