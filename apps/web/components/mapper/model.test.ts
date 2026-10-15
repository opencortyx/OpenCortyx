/**
 * The Materialized graph must draw what ingestion builds from the SAME sample.
 *
 * It drew nothing for `locomo_test` — a mapping whose data had ingested 10 dialogues and 6,079 turns.
 * `walk()` read every rule path literally, while the Python walker it previews does not:
 *
 *   1. `payload_root[]` is the editor's ROOT SENTINEL (schema.canonical_path folds it away). The preview
 *      looked for a key literally named `payload_root` inside each record, found none, and so every
 *      rule — the root included — matched nothing.
 *   2. `*` is a wildcard within ONE segment (ingest_json._glob_to_re: `[^.\[\]]*`). The preview looked
 *      up a key literally named `session_*`; the data has `session_1`, `session_2`, ...
 *   3. When `*` does expand, the trail has to carry the REAL key. A trail of `session_*[0]` is identical
 *      for every session, and `byTrail` is keyed on it — so turns would attach to the wrong parent.
 *   4. Where an exact rule and a pattern both match one path, the MOST SPECIFIC rule wins
 *      (ingest_json._PathIndex), rather than both producing a node.
 *
 * The fixture mirrors the saved sample's real shape (2 records; numbered `session_N` arrays sitting next to
 * `session_N_date_time` strings; `img_url` lists on some turns), cut down so the counts are checkable by eye.
 * Rules are node rows only, so node ids come from trails and never collide across records.
 */
import { describe, expect, it } from "@jest/globals";
import type { EdgeRule, KeyRow } from "./model";
import { materialize, walk } from "./model";

const SAMPLE = [
  {
    qa: [{ question: "q1", answer: "a1", evidence: ["D1:1"], category: 1 }],
    conversation: {
      speaker_a: "Joanna",
      speaker_b: "Nate",
      session_1_date_time: "7:31 pm on 21 January, 2022",
      session_1: [
        { speaker: "Joanna", dia_id: "D1:1", text: "hi", img_url: ["http://x/1.png", "http://x/2.png"] },
        { speaker: "Nate", dia_id: "D1:2", text: "hello" },
      ],
      session_2_date_time: "8:00 pm on 22 January, 2022",
      session_2: [{ speaker: "Joanna", dia_id: "D2:1", text: "again" }],
    },
    event_summary: { events_session_1: { Joanna: ["e1"], Nate: [], date: "21 January, 2022" } },
    observation: { session_1_observation: { Joanna: [["o1", "D1:1"]], Nate: [] } },
    session_summary: { session_1_summary: "summary one" },
    sample_id: "conv-1",
  },
  {
    qa: [],
    conversation: {
      speaker_a: "Ann",
      speaker_b: "Bo",
      session_1_date_time: "9:00 am on 1 March, 2022",
      session_1: [{ speaker: "Ann", dia_id: "D1:1", text: "yo" }],
    },
    event_summary: {},
    observation: {},
    session_summary: {},
    sample_id: "conv-2",
  },
];

const node = (
  becomes: string, node_type: string, path: string, edge_type: string, attachToNode?: string
): KeyRow => ({
  kind: "node", becomes, decision: "node", node_type, path, edge_type,
  ...(attachToNode ? { attachToNode } : {}),
});

/** The `locomo_test` rules as saved: 4 active, the rest set to Ignore. */
const LOCOMO_ROWS: KeyRow[] = [
  node("node", "Dialogue", "payload_root", "ROOT"),
  node("ignore", "Qa", "payload_root[].qa[]", "CONTAINS_QA", "Record"),
  node("node", "Conversation", "payload_root[].conversation", "HAS_CONVERSATION", "Record"),
  node("node", "Turn", "payload_root[].conversation.session_*[]", "HAS_TURN", "Conversation"),
  node("node", "Img_url", "payload_root[].conversation.session_*[].img_url[]", "CONTAINS_IMG_URL", "Session_9"),
  node("ignore", "Event_summary", "payload_root[].event_summary", "HAS_EVENT_SUMMARY", "Record"),
  node("ignore", "Session_observation", "payload_root[].observation.session_*_observation", "HAS_SESSION_OBSERVATION", "Observation"),
];

const LOCOMO_EDGES: EdgeRule[] = [
  { id: "er-6", fromNode: "Record", toNode: "Conversation", direction: "out", edgeLabel: "HAS_CONVERSATION" },
  { id: "er-10", fromNode: "Conversation", toNode: "Turn", direction: "out", edgeLabel: "HAS_TURN" },
  { id: "er-14", fromNode: "Turn", toNode: "Img_url", direction: "out", edgeLabel: "CONTAINS_IMG_URL" },
];

const countBy = <T>(xs: T[], key: (x: T) => string) =>
  xs.reduce<Record<string, number>>((acc, x) => { acc[key(x)] = (acc[key(x)] || 0) + 1; return acc; }, {});

/** `Turn@$[0].conversation.session_1[0]` → `$[0]`: which record a trail-identified node came from. */
const recordOf = (id: string) => id.split("@")[1].split(".")[0];

describe("walk — the root sentinel is folded, exactly as schema.canonical_path does", () => {
  it("matches each record of a top-level array for a root rule", () => {
    expect(walk("payload_root", SAMPLE)).toHaveLength(2);
    expect(walk("payload_root[]", SAMPLE)).toHaveLength(2);
  });

  it("matches a single-object payload for every root alias", () => {
    for (const alias of ["payload_root", "$root", "__root__", "root_payload"]) {
      expect(walk(alias, SAMPLE[0])).toHaveLength(1);
    }
  });

  it("walks the record itself, not a key literally named payload_root", () => {
    expect(walk("payload_root[].conversation", SAMPLE)).toHaveLength(2);
    expect(walk("payload_root.conversation", SAMPLE[0])).toHaveLength(1);
  });
});

describe("walk — `*` matches within one segment, exactly as ingest_json._glob_to_re does", () => {
  it("expands a numbered key across every session in every record", () => {
    expect(walk("payload_root[].conversation.session_*[]", SAMPLE)).toHaveLength(4);
  });

  it("never mistakes a string sibling for the array its name resembles", () => {
    // `session_1_date_time` also matches the NAME `session_*`; `[]` demands an array, so it must not count.
    const texts = walk("payload_root[].conversation.session_*[]", SAMPLE).map((h) => h.value.text);
    expect(texts).toEqual(["hi", "hello", "again", "yo"]);
  });

  it("puts the real key in every trail, so two sessions can never share one", () => {
    const trails = walk("payload_root[].conversation.session_*[]", SAMPLE).map((h) => h.trail.join("."));
    expect(new Set(trails).size).toBe(trails.length);
    expect(trails).toContain("$[0].conversation.session_2[0]");
    expect(trails.some((t) => t.includes("*"))).toBe(false);
  });

  it("reaches through a wildcard to the level beneath it", () => {
    expect(walk("payload_root[].conversation.session_*[].img_url[]", SAMPLE)).toHaveLength(2);
  });

  it("matches a wildcard in the middle of a key", () => {
    expect(walk("payload_root[].observation.session_*_observation", SAMPLE)).toHaveLength(1);
    expect(walk("payload_root[].event_summary.events_session_*", SAMPLE)).toHaveLength(1);
  });

  it("matches every key of an object for a lone `*`", () => {
    // record 1: speaker_a, speaker_b, 2 date strings, 2 session arrays = 6; record 2: 4
    expect(walk("payload_root[].conversation.*", SAMPLE)).toHaveLength(10);
  });

  it("does not let `*` cross a dot", () => {
    // turns sit two levels under a top-level key; a single-segment `*` must not reach them
    expect(walk("payload_root[].*.text", SAMPLE)).toHaveLength(0);
  });
});

describe("walk — `**` matches across segments", () => {
  it("finds a key at any depth, through objects and arrays", () => {
    expect(walk("payload_root[].**.dia_id", SAMPLE).map((h) => h.value)).toEqual(["D1:1", "D1:2", "D2:1", "D1:1"]);
  });
});

describe("materialize — the real locomo_test mapping draws its graph", () => {
  const g = materialize(LOCOMO_ROWS, LOCOMO_EDGES, SAMPLE);

  it("produces the four active types from the saved sample's shape", () => {
    expect(countBy(g.nodes, (n) => n.type)).toEqual({ Dialogue: 2, Conversation: 2, Turn: 4, Img_url: 2 });
  });

  it("draws the containment chain", () => {
    expect(countBy(g.edges, (e) => e.type)).toEqual({ HAS_CONVERSATION: 2, HAS_TURN: 4, CONTAINS_IMG_URL: 2 });
  });

  it("attaches every turn to the conversation of its OWN record", () => {
    const hasTurn = g.edges.filter((e) => e.type === "HAS_TURN");
    expect(hasTurn.length).toBeGreaterThan(0);
    for (const e of hasTurn) expect(recordOf(e.source)).toBe(recordOf(e.target));
  });

  it("attaches a Conversation to the root, even though its rule still names the old `Record` parent", () => {
    const hasConv = g.edges.filter((e) => e.type === "HAS_CONVERSATION");
    expect(hasConv.map((e) => e.source.split("@")[0])).toEqual(["Dialogue", "Dialogue"]);
  });

  it("produces nothing for a rule set to Ignore", () => {
    const types = new Set(g.nodes.map((n) => n.type));
    for (const t of ["Qa", "Event_summary", "Session_observation"]) expect(types.has(t)).toBe(false);
  });
});

describe("materialize — the most specific rule wins, exactly as ingest_json._PathIndex does", () => {
  const rows: KeyRow[] = [
    node("node", "Root", "payload_root", "ROOT"),
    node("node", "Conv", "payload_root[].conversation", "HAS_CONV"),
    node("node", "FirstSession", "payload_root[].conversation.session_1[]", "HAS_FIRST"),
    node("node", "Session", "payload_root[].conversation.session_*[]", "HAS_SESSION"),
  ];
  const g = materialize(rows, [], SAMPLE);

  it("types session_1 by the exact rule and every other session by the pattern", () => {
    const byType = countBy(g.nodes, (n) => n.type);
    expect(byType.FirstSession).toBe(3);
    expect(byType.Session).toBe(1);
  });

  it("never produces two nodes for one element that two rules both match", () => {
    const turnish = g.nodes.filter((n) => n.type === "FirstSession" || n.type === "Session");
    expect(turnish).toHaveLength(4);
  });
});

describe("materialize — says what the preview cannot show, instead of dropping it", () => {
  it("warns about an attribute whose field name is a wildcard", () => {
    const rows: KeyRow[] = [
      node("node", "Root", "payload_root", "ROOT"),
      node("node", "Conv", "payload_root[].conversation", "HAS_CONV"),
      { kind: "attribute", becomes: "date", path: "payload_root[].conversation.session_*_date_time", attr_name: "date" },
    ];
    const g = materialize(rows, [], SAMPLE);
    expect(g.warnings.some((w) => w.includes("session_*_date_time"))).toBe(true);
  });
});
