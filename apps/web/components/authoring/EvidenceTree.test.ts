import { describe, expect, it } from "@jest/globals";
import { parseEvidenceTree } from "./EvidenceTree";
import type { DraftEvidence } from "@/lib/api/contextV2";

describe("parseEvidenceTree", () => {
  it("parses hierarchical entity evidence into a tree of nodes and attributes", () => {
    const evidence: DraftEvidence[] = [
      {
        claim: "programme.budget_eur → a label kept on the node",
        basis: "int: '4200000'",
        path: "programme.budget_eur",
      },
      {
        claim: "programme.name → a label kept on the node",
        basis: "str: 'Atlas Modernisation'",
        path: "programme.name",
      },
      {
        claim: "programme.programme_id → an identifier",
        basis: "str: 'PRG-7'",
        path: "programme.programme_id",
      },
      {
        claim: "programme.projects[].lead.discipline → a label kept on the node",
        basis: "str: 'backend'",
        path: "programme.projects[].lead.discipline",
      },
      {
        claim: "programme.projects[].lead.name → a label kept on the node",
        basis: "str: 'Marek Nowak'",
        path: "programme.projects[].lead.name",
      },
      {
        claim: "programme.projects[].milestones[].milestone_id → an identifier",
        basis: "str: 'MS-1'",
        path: "programme.projects[].milestones[].milestone_id",
      },
      {
        claim: "programme becomes a Programme node",
        basis: "object branch with 3 scalar field(s)",
        path: "programme",
      },
      {
        claim: "programme.projects[] becomes a Project node",
        basis: "array branch with 4 scalar field(s)",
        path: "programme.projects[]",
      },
      {
        claim: "programme.projects[].lead becomes a Lead node",
        basis: "object branch with 3 scalar field(s)",
        path: "programme.projects[].lead",
      },
      {
        claim: "programme.projects[].milestones[] becomes a Milestone node",
        basis: "array branch with 3 scalar field(s)",
        path: "programme.projects[].milestones[]",
      },
    ];

    const result = parseEvidenceTree(evidence);
    expect(result.isTree).toBe(true);
    expect(result.roots.length).toBe(1);

    const programme = result.roots[0];
    expect(programme.nodeType).toBe("Programme");
    expect(programme.path).toBe("programme");
    expect(programme.attributes.map((a) => a.name)).toEqual(
      expect.arrayContaining(["budget_eur", "name", "programme_id"])
    );

    // Check children under Programme
    expect(programme.children.length).toBe(1);
    const project = programme.children[0];
    expect(project.nodeType).toBe("Project");
    expect(project.path).toBe("programme.projects[]");

    // Check children under Project
    expect(project.children.length).toBe(2);
    const lead = project.children.find((c) => c.nodeType === "Lead");
    const milestone = project.children.find((c) => c.nodeType === "Milestone");

    expect(lead).toBeDefined();
    expect(lead!.attributes.map((a) => a.name)).toEqual(
      expect.arrayContaining(["discipline", "name"])
    );

    expect(milestone).toBeDefined();
    expect(milestone!.attributes[0].name).toBe("milestone_id");
    expect(milestone!.attributes[0].sampleValue).toBe("MS-1");
  });

  it("parses structured CSV column evidence into an entity node card", () => {
    const evidence: DraftEvidence[] = [
      {
        claim: "customers becomes a Customer node",
        basis: "one Customer entity node per table row (4 columns)",
        path: "customers",
      },
      {
        claim: "id → the row's identity",
        basis: "string column, named like a key",
        path: "customers.id",
      },
      {
        claim: "full_name → a label kept on the node",
        basis: "string column",
        path: "customers.full_name",
      },
      {
        claim: "city → a label kept on the node",
        basis: "string column",
        path: "customers.city",
      },
      {
        claim: "signup_date → a label kept on the node",
        basis: "date column",
        path: "customers.signup_date",
      },
    ];

    const result = parseEvidenceTree(evidence);
    expect(result.isTree).toBe(true);
    expect(result.roots.length).toBe(1);

    const customer = result.roots[0];
    expect(customer.nodeType).toBe("Customer");
    expect(customer.attributes.length).toBe(4);
    expect(customer.attributes.map((a) => a.name)).toEqual(
      expect.arrayContaining(["id", "full_name", "city", "signup_date"])
    );
  });

  it("parses document evidence into Document, Section, and Chunk hierarchy without synthetic stages", () => {
    const evidence: DraftEvidence[] = [
      {
        claim: "document becomes a Document node",
        basis: "root entity for onboarding.md (2,400 chars, 450 words)",
        path: "document",
      },
      {
        claim: "document.title → an identifier",
        basis: "str: 'onboarding.md'",
        path: "document.title",
      },
      {
        claim: "document.file_type → a label kept on the node",
        basis: "str: 'text'",
        path: "document.file_type",
      },
      {
        claim: "document.sections[] becomes a Section node",
        basis: "2 unique heading section(s) discovered in document",
        path: "document.sections",
      },
      {
        claim: "document.sections[].heading → a label kept on the node",
        basis: "str: 'Getting Started'",
        path: "document.sections[].heading",
      },
      {
        claim: "document.sections[].chunks[] becomes a Chunk node",
        basis: "split into 3 passage chunk(s) via markdown_heading",
        path: "document.sections[].chunks",
      },
      {
        claim: "document.sections[].chunks[].text → prose that gets read and extracted from",
        basis: "str: 'Welcome to the engineering handbook...'",
        path: "document.sections[].chunks[].text",
      },
      {
        claim: "document.sections[].chunks[].ordinal → an identifier",
        basis: "int: '0'",
        path: "document.sections[].chunks[].ordinal",
      },
    ];

    const result = parseEvidenceTree(evidence);
    expect(result.isTree).toBe(true);
    expect(result.roots.length).toBe(1);

    const doc = result.roots[0];
    expect(doc.nodeType).toBe("Document");
    expect(doc.attributes.map((a) => a.name)).toEqual(expect.arrayContaining(["title", "file_type"]));
    expect(doc.children.length).toBe(1);

    const section = doc.children[0];
    expect(section.nodeType).toBe("Section");
    expect(section.attributes.map((a) => a.name)).toContain("heading");
    expect(section.children.length).toBe(1);

    const chunk = section.children[0];
    expect(chunk.nodeType).toBe("Chunk");
    expect(chunk.attributes.map((a) => a.name)).toEqual(expect.arrayContaining(["text", "ordinal"]));
    expect(chunk.children.length).toBe(0);
  });

  it("separates relationship edges from node attributes", () => {
    const evidence: DraftEvidence[] = [
      {
        claim: "orders becomes a Order node",
        basis: "one Order entity node per table row (3 columns)",
        path: "orders",
      },
      {
        claim: "id → the row's identity",
        basis: "int column, named like a key",
        path: "orders.id",
      },
      {
        claim: "customer_id → a link to another table",
        basis: "int column, named like a key",
        path: "orders.customer_id",
      },
      {
        claim: "orders.customer_id becomes a Customer node",
        basis: "referenced Customer entity via foreign key",
        path: "orders.customer_id",
      },
      {
        claim: "Order -HAS_CUSTOMER-> Customer",
        basis: "customer_id is shaped like a foreign key to Customer",
        path: "orders.customer_id",
      },
    ];

    const result = parseEvidenceTree(evidence);
    expect(result.isTree).toBe(true);
    expect(result.roots.length).toBe(1);

    const order = result.roots[0];
    expect(order.nodeType).toBe("Order");
    // customer_id is a child node
    expect(order.children.length).toBe(1);
    expect(order.children[0].nodeType).toBe("Customer");

    // The edge Order -HAS_CUSTOMER-> Customer is in relationships, NOT attributes
    expect(result.relationships.length).toBe(1);
    expect(result.relationships[0].claim).toBe("Order -HAS_CUSTOMER-> Customer");
    expect(order.attributes.some((a) => a.name.includes("HAS_CUSTOMER"))).toBe(false);
  });

  it("gracefully falls back to flat list if no nodes are detected", () => {
    const evidence: DraftEvidence[] = [
      {
        claim: "starts from Default Pipeline 2",
        basis: "semistructured routing",
      },
      {
        claim: "removed 2 language-model stages",
        basis: "no long prose text",
      },
    ];

    const result = parseEvidenceTree(evidence);
    expect(result.isTree).toBe(false);
    expect(result.roots.length).toBe(0);
    expect(result.unattached.length).toBe(2);
  });
});
