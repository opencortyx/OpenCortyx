import { describe, expect, it } from "@jest/globals";
import { buildStages, readingRule, splitStages, type PipelineStage } from "./model";

describe("reading rule", () => {
  it("keeps only the mapping and the two options", () => {
    expect(readingRule({
      mapping_ids: ["m1"], strategy: "markdown", chunk_size: 1200, chunk_overlap: 150,
      node_type: "Chunk", tables: [{ table: "t" }], id_patterns: ["ref"], strict: true,
    })).toEqual({ mapping_ids: ["m1"] });
  });

  it("reads options saved under their earlier names", () => {
    expect(readingRule({ mapping_id: "m1", save_chunks: false })).toEqual({ mapping_ids: ["m1"], extract_only: true });
    expect(readingRule({ structure_only: true, auto_accept: false })).toEqual({ auto_accept: false, extract_only: true });
    expect(readingRule({ mode: "catalog" })).toEqual({ extract_only: true });
    expect(readingRule({ mode: "auto", auto_accept: true })).toEqual({});
    expect(readingRule(undefined)).toEqual({});
  });

  it("clears removed settings on save without re-keying the anchor", () => {
    const saved: PipelineStage[] = [
      { id: "stage_0", type: "chunking", source: "document",
        config: { strategy: "markdown", chunk_size: 1200, auto_accept: false } },
      { id: "stage_1", type: "builtin:py.date_normalizer", config: {} },
    ];
    const { anchor, anchorStage, steps } = splitStages(saved, "document");
    expect(anchor).toEqual({ auto_accept: false });

    const stages = buildStages("document", { ...anchor, extract_only: true }, steps, anchorStage);
    expect(stages[0]).toEqual({
      id: "stage_0", type: "chunking", source: "document", config: { auto_accept: false, extract_only: true },
    });
    expect(stages[1]).toBe(saved[1]);
  });

  it("writes the canonical anchor for a new pipeline", () => {
    expect(buildStages("structured", { extract_only: true, tables: [] }, [])[0])
      .toEqual({ id: "stage_0", action: "base_schema", config: { extract_only: true } });
  });
});
