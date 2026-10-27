"use client";

import { useEffect, useState } from "react";
import { contextV2Api, Connector, ConnectorSpec, SecretRef } from "@/lib/api/contextV2";
import {
  Cable, Database, Plus, Trash2, Pencil, Plug, KeyRound, X, CheckCircle2, AlertCircle,
  Loader2, Table2, ChevronDown, ChevronUp, FileCode, ShieldCheck, Bot, Copy, Terminal, Sparkles, Search, HelpCircle
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

type SecretProvider = "inline" | "azure_keyvault" | "env";

type Family = "sql" | "lake" | "format";
const ENGINES: { value: string; label: string; server?: boolean; uri: string; family?: Family }[] = [
  { value: "s3", label: "AWS S3 (data lake)", family: "lake", uri: "" },
  { value: "gcs", label: "Google Cloud Storage (data lake)", family: "lake", uri: "" },
  { value: "adls", label: "Azure ADLS (data lake)", family: "lake", uri: "" },
  { value: "local", label: "On-prem files (local data lake)", family: "lake", uri: "" },
  { value: "postgres", label: "PostgreSQL", server: true, uri: "postgresql://user:pass@host:5432/db" },
  { value: "mysql", label: "MySQL", server: true, uri: "mysql://user:pass@host:3306/db" },
  { value: "sqlite", label: "SQLite (file)", uri: "sqlite:////app/spider2-databases/chinook.sqlite" },
  { value: "mssql", label: "SQL Server", uri: "mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+18+for+SQL+Server" },
  { value: "oracle", label: "Oracle", uri: "oracle+oracledb://user:pass@host:1521/?service_name=XEPDB1" },
  { value: "mongodb", label: "MongoDB (read in place)", server: true, uri: "mongodb://reader:pass@host:27017/db" },
];
const ENGINE = (k: string) => ENGINES.find((e) => e.value === k) || ENGINES[ENGINES.length - 1];
const FAMILY = (k: string): Family => ENGINE(k).family || "sql";

const EMPTY_FORM = {
  id: "" as string,
  name: "",
  kind: "postgres" as string,
  host: "",
  port: "" as string,
  database: "",
  username: "",
  connectionString: "",
  optAccessKeyId: "",
  optRegion: "",
  authMethod: "",
  accountName: "",
  tenantId: "",
  clientId: "",
  certPath: "",
  assumeRoleArn: "",
  chain: "",
  lakeConnectorId: "",
  directory: "",
  queryEngineId: "",
  secretProvider: "inline" as SecretProvider,
  secretValue: "",
  secretVar: "",
  vaultUrl: "",
  secretName: "",
};

type AgentKind = "config" | "code" | "remote";

interface CatalogConnector {
  id: string;
  name: string;
  category: string;
  kind: string;
  onPrem?: boolean;
}

interface McpClient {
  id: string;
  name: string;
  subtitle: string;
  description: string;
  whyInDemand: string;
  setupSteps: string[];
  configJson?: string;
  cmdBlock?: string;
  // Which transports this client actually supports. Some clients (Claude Desktop) can ONLY launch a
  // local process and reject a URL-based entry, so the guide must show them different snippets.
  supports?: ("remote" | "local")[];
  localSteps?: string[];          // stdio setup, when it differs from the remote flow
  localConfigJson?: string;       // stdio config block
  localCmdBlock?: string;
  note?: string;                  // client-specific gotcha worth calling out
}

// The single source of truth for the endpoint, so every snippet on this page agrees.
const MCP_ENDPOINT = "http://localhost:8001/mcp";

// Reusable snippets. `YOUR_API_KEY_HERE` is substituted with a freshly generated key in the guide.
const REMOTE_CONFIG = `{
  "mcpServers": {
    "opencortyx": {
      "type": "http",
      "url": "${MCP_ENDPOINT}",
      "headers": {
        "X-API-Key": "YOUR_API_KEY_HERE"
      }
    }
  }
}`;

// stdio against the running container — no local Python, no port, no key in the config (the
// container already holds one). Grants the client docker-exec rights, so it is a LOCAL-only option.
const DOCKER_STDIO_CONFIG = `{
  "mcpServers": {
    "opencortyx": {
      "command": "docker",
      "args": ["exec", "-i", "-e", "MCP_TRANSPORT=stdio", "opencortyx-mcp-1", "python", "server.py"]
    }
  }
}`;

const CATEGORIES = [
  { id: "all", name: "All Connectors" },
  { id: "databases", name: "Databases" },
  { id: "lakehouse", name: "Data Lakehouses" },
];

const CATALOG_CONNECTORS: CatalogConnector[] = [
  { id: "postgres", name: "PostgreSQL", category: "Databases", kind: "postgres" },
  { id: "mysql", name: "MySQL", category: "Databases", kind: "mysql" },
  { id: "sqlite", name: "SQLite", category: "Databases", kind: "sqlite" },
  { id: "mssql", name: "SQL Server", category: "Databases", kind: "mssql" },
  { id: "oracle", name: "Oracle", category: "Databases", kind: "oracle" },
  { id: "mongodb", name: "MongoDB", category: "Databases", kind: "mongodb" },
  { id: "s3", name: "AWS S3", category: "Data Lakehouses", kind: "s3" },
  { id: "gcs", name: "Google Cloud Storage", category: "Data Lakehouses", kind: "gcs" },
  { id: "adls", name: "Azure ADLS", category: "Data Lakehouses", kind: "adls" },
  { id: "local", name: "On-prem files", category: "Data Lakehouses", kind: "local", onPrem: true },
];

const MCP_CLIENTS: McpClient[] = [
  {
    id: "openclaw",
    name: "OpenClaw",
    subtitle: "Autonomous Local Agent Framework",
    description: "Highly viral, open-source, autonomous AI agent framework designed to run locally. Acts as an active assistant that can control your browser, run commands, and execute complex workflows.",
    whyInDemand: "Integrating OpenClaw with OpenCortyx gives it a unified long-term memory, vector search, and a relational knowledge graph of all your local files and databases.",
    supports: ["remote"],
    setupSteps: [
      "Generate a workspace API key above — the server refuses any request without one.",
      "Locate your OpenClaw configuration file (typically mcp.json at ~/.openclaw/mcp.json or your gateway settings).",
      "Add the opencortyx server definition shown in the configuration block below.",
      "Restart the OpenClaw gateway daemon to apply configuration changes.",
      "OpenClaw will start calling OpenCortyx tools (opencortyx_search, opencortyx_remember, …) to retrieve and record enterprise knowledge."
    ],
    configJson: REMOTE_CONFIG,
    cmdBlock: "openclaw gateway restart"
  },
  {
    id: "open_webui",
    name: "Open WebUI",
    subtitle: "Local Web Chat Interface for LLMs",
    description: "The most famous self-hosted web interface for local LLMs (like Ollama, Llama 3, DeepSeek, and Qwen). Let users query documents, schema catalogs, and graph architectures via local chat.",
    whyInDemand: "Allows you to run a fully private, ChatGPT-like web UI experience powered entirely by local models with direct access to private enterprise files.",
    supports: ["remote"],
    setupSteps: [
      "Generate a workspace API key above.",
      "Sign in to your Open WebUI Admin Panel.",
      "Navigate to Settings > External Tools.",
      "Add a new tool connection and select the connection type as MCP Streamable HTTP.",
      `Enter the OpenCortyx endpoint URL: ${MCP_ENDPOINT}`,
      "Provide the X-API-Key header with your generated workspace API key.",
      "Save the tool connection. Open WebUI will register the custom functions and models will call them dynamically."
    ]
  },
  {
    id: "cursor_cline",
    name: "Cursor & Cline (Roo Code)",
    subtitle: "IDE Coding Agent Extensions",
    description: "IDE extensions and AI editors that can query documentation, catalog schemas, and architectural graphs right inside the developer workspace.",
    whyInDemand: "Having an AI agent in the IDE that can query code and schema context is a massive productivity booster.",
    supports: ["remote"],
    setupSteps: [
      "Generate a workspace API key above.",
      `For Cursor: Settings > Features > MCP > Add New MCP Server. Choose the HTTP (streamable) type, name it 'opencortyx', enter ${MCP_ENDPOINT}, and add the X-API-Key header.`,
      "For VS Code (Cline / Roo Code): open the extension settings panel or the Cline configuration folder.",
      "Add the opencortyx definition inside the mcpServers configuration object (shown below).",
      "Save the configuration — Cline/Roo Code will pick up the tools on its next reload."
    ],
    configJson: REMOTE_CONFIG
  },
  {
    id: "claude",
    name: "Claude Desktop & Claude Code",
    subtitle: "Anthropic's Official MCP Clients",
    description: "The official command-line and desktop interfaces built by Anthropic, the team that designed and created the Model Context Protocol.",
    whyInDemand: "Maintained by the team that created MCP, with built-in support for MCP tool calls in Claude.",
    supports: ["remote", "local"],
    note: "Claude CODE speaks HTTP. Claude DESKTOP does not: its mcpServers entries launch a local "
      + "process, so a \"type\": \"http\" entry is rejected as an invalid config. Use the Local tab for Desktop.",
    setupSteps: [
      "Generate a workspace API key above.",
      "Claude Code: run the CLI command below (or put the JSON in .mcp.json at your project root).",
      "Claude Code reads the key from the header on every call — each agent should have its own key.",
      "Restart / reload Claude Code to pick up the tools."
    ],
    configJson: REMOTE_CONFIG,
    cmdBlock: `claude mcp add --transport http opencortyx ${MCP_ENDPOINT} --header "X-API-Key: YOUR_API_KEY_HERE"`,
    localSteps: [
      "Quit Claude Desktop completely (tray icon > Quit — closing the window is not enough, it rewrites the config on exit).",
      "Windows: edit %APPDATA%/Claude/claude_desktop_config.json — macOS: ~/Library/Application Support/Claude/claude_desktop_config.json",
      "Paste the block below under the mcpServers key, keeping any other entries you already have.",
      "This runs the MCP server inside the already-running container over stdio, so no API key goes in the config file and no port is used.",
      "Relaunch Claude Desktop. The opencortyx tools appear in the tools menu."
    ],
    localConfigJson: DOCKER_STDIO_CONFIG
  },
  {
    id: "ollama",
    name: "Ollama (Local LLM Orchestrator)",
    subtitle: "Local Model Runner",
    description: "Popular local model orchestrator allowing you to run DeepSeek, Llama 3, Qwen, and other open LLMs entirely on your workstation.",
    whyInDemand: "Powers local completion models. Combining Ollama with an MCP client (such as Open WebUI or Cursor) allows for a fully offline and private AI workspace.",
    supports: ["remote"],
    note: "Ollama is not an MCP client itself — it runs the model. Point an MCP-capable front end "
      + "(Open WebUI, Cline, …) at OpenCortyx and select an Ollama model there.",
    setupSteps: [
      "Verify that Ollama is installed and running locally on port 11434.",
      "Register OpenCortyx in your MCP-capable front end (e.g. Open WebUI or Cline) using the remote endpoint and your API key.",
      "Select your local Ollama model (e.g. llama3, deepseek-r1) as the active completion model.",
      "Run prompts. The client will query local LLMs, forwarding tool requests to OpenCortyx."
    ]
  },
  {
    id: "crewai",
    name: "CrewAI (Multi-Agent Framework)",
    subtitle: "Autonomous Agent Orchestrator",
    description: "Framework for building multi-agent systems where multiple specialized roles cooperate to solve a set of complex business problems.",
    whyInDemand: "Allows defining teams of agents. Equipping them with OpenCortyx tools lets them perform structured agentic RAG across connected databases.",
    supports: ["remote"],
    setupSteps: [
      "Generate one API key PER AGENT ROLE so each crew member's actions are separately attributed and revocable.",
      "Instantiate an MCP client in your codebase (python or typescript).",
      `Connect it to ${MCP_ENDPOINT} with the X-API-Key header set to that agent's key.`,
      "Map the discovered OpenCortyx tools into CrewAI tools.",
      "Give read-only crew members a viewer key — they will be refused any write tool by the server, not just by convention."
    ]
  }
];

function getConnectorDescription(c: CatalogConnector): string {
  switch (c.category) {
    case "Databases":
      return `Connect to your ${c.name} SQL database, introspect relational schemas, and sync catalog metadata.`;
    case "Data Warehouses":
      return `Sync tables, views, and execution metrics from your ${c.name} cloud data warehouse.`;
    case "Data Lakehouses":
      return `Query Delta, Iceberg, or Hudi table formats stored on your ${c.name} data lake storage.`;
    case "CRM":
      return `Integrate customer details, sales history, and CRM pipeline events from ${c.name}.`;
    case "BI Tools":
      return `Synchronize visualization metrics, dashboards, and custom reporting data with ${c.name}.`;
    case "Data Governance":
      return `Enforce compliance rules, audit access, and track catalog lineage metadata in ${c.name}.`;
    case "Data Quality & Observability":
      return `Track SLA commitments, null rates, validation incidents, and data freshness with ${c.name}.`;
    case "ERP":
      return `Connect your enterprise resource planning databases and ledger schemas with ${c.name}.`;
    case "ETL Tools":
      return `Orchestrate pipeline runs, transformation schedules, and ingestion workflows in ${c.name}.`;
    case "Event/Messaging":
      return `Stream real-time message topics, event logs, and queue metrics from your ${c.name} instance.`;
    case "Knowledge Management":
      return `Sync markdown files, notebooks, pages, and enterprise directories recursively from ${c.name}.`;
    default:
      return `Connect to ${c.name} and configure data flows, schema syncs, and metadata ingestion.`;
  }
}

function getKindDisplayName(kind: string): string {
  const mapping: Record<string, string> = {
    local: "On-prem files",
    postgres: "PostgreSQL",
    mysql: "MySQL",
    sqlite: "SQLite",
    mssql: "SQL Server",
    oracle: "Oracle",
    mongodb: "MongoDB",
    s3: "AWS S3",
    adls: "Azure ADLS",
    gcs: "Google Cloud Storage",
  };
  return mapping[kind] || kind;
}

function ConnectorLogo({ name }: { name: string }) {
  const n = name.toLowerCase();
  
  if (n.includes("on-prem files") || n === "local") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        {/* Monitor Screen Frame */}
        <rect x="18" y="24" width="48" height="34" rx="4" fill="#4B5563" fillOpacity="0.1" stroke="#4B5563" strokeWidth="5" />
        {/* Inner Screen Blue glass */}
        <rect x="23" y="29" width="38" height="24" fill="#60A5FA" />
        {/* Monitor Stand neck */}
        <path d="M42 58 V70" stroke="#4B5563" strokeWidth="6" strokeLinecap="round" />
        {/* Monitor Stand base */}
        <path d="M30 70 H54" stroke="#4B5563" strokeWidth="6" strokeLinecap="round" />
        
        {/* Computer Tower adjacent */}
        <rect x="70" y="20" width="15" height="50" rx="2" fill="#9CA3AF" fillOpacity="0.15" stroke="#4B5563" strokeWidth="5" />
        {/* CD-ROM Drive slot */}
        <line x1="74" y1="28" x2="81" y2="28" stroke="#4B5563" strokeWidth="4" strokeLinecap="round" />
        {/* Power LED Red */}
        <circle cx="77" cy="45" r="2.5" fill="#EF4444" />
        {/* Disk LED Green */}
        <circle cx="77" cy="54" r="2" fill="#10B981" />
      </svg>
    );
  }
  if (n === "altr") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M15 80 L35 20 L50 60 L65 20 L85 80" stroke="black" strokeWidth="12" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M30 65 L70 65" stroke="black" strokeWidth="8" strokeLinecap="round"/>
      </svg>
    );
  }
  if (n.includes("aws glue")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="28" height="28" rx="6" fill="#E67E22" />
        <rect x="57" y="15" width="28" height="28" rx="6" fill="#E67E22" />
        <rect x="36" y="57" width="28" height="28" rx="6" fill="#E67E22" />
        <path d="M36 29 H64 M29 43 V71 M71 43 V71" stroke="#F39C12" strokeWidth="4" strokeDasharray="4 2" />
      </svg>
    );
  }
  if (n.includes("sagemaker")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 15 C30 15 20 30 20 50 C20 70 30 85 50 85 C70 85 80 70 80 50 C80 30 70 15 50 15 Z" fill="#2ECC71" fillOpacity="0.15" stroke="#2ECC71" strokeWidth="6" />
        <path d="M40 38 C40 33 45 30 50 30 C55 30 60 33 60 38 C60 42 55 45 50 48 V55" stroke="#27AE60" strokeWidth="6" strokeLinecap="round" />
        <circle cx="50" cy="68" r="5" fill="#27AE60" />
      </svg>
    );
  }
  if (n.includes("aiven kafka") || n.includes("aiven")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="55" r="28" fill="#FF5733" />
        <circle cx="40" cy="30" r="8" fill="white" stroke="#FF5733" strokeWidth="3" />
        <circle cx="40" cy="30" r="3" fill="black" />
        <circle cx="60" cy="30" r="8" fill="white" stroke="#FF5733" strokeWidth="3" />
        <circle cx="60" cy="30" r="3" fill="black" />
        <path d="M25 50 C12 40 10 60 20 65" stroke="#FF5733" strokeWidth="5" strokeLinecap="round" />
        <path d="M75 50 C88 40 90 60 80 65" stroke="#FF5733" strokeWidth="5" strokeLinecap="round" />
        <path d="M30 75 C20 85 35 90 40 80" stroke="#FF5733" strokeWidth="4" strokeLinecap="round" />
        <path d="M70 75 C80 85 65 90 60 80" stroke="#FF5733" strokeWidth="4" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("alloydb")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="42" fill="#3498DB" fillOpacity="0.1" stroke="#3498DB" strokeWidth="4" />
        <path d="M68 62 C68 50 60 40 50 40 C43 40 37 45 35 50 C32 50 28 53 28 57 C28 62 34 65 38 65" stroke="#2980B9" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M50 40 C52 32 60 28 68 28 C74 28 80 34 80 42 C80 50 74 58 68 58" stroke="#1A5276" strokeWidth="4" />
      </svg>
    );
  }
  if (n === "alteryx") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="42" fill="#1F4E79" />
        <text x="50" y="60" textAnchor="middle" fill="white" fontSize="42" fontFamily="sans-serif" fontWeight="bold">a</text>
      </svg>
    );
  }
  if (n.includes("postgres")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M25 45 C25 25 40 20 50 20 C65 20 75 35 75 55 C75 70 65 80 50 80 C40 80 35 75 30 70" stroke="#336791" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M45 40 C42 40 38 45 38 50 C38 55 42 60 45 60" stroke="#336791" strokeWidth="4" />
        <path d="M50 80 C50 80 55 90 40 90" stroke="#336791" strokeWidth="4" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("mysql")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 50 C20 30 35 15 55 15 C70 15 80 25 80 35 C80 45 70 50 60 52 C50 54 40 60 40 70" stroke="#00758F" strokeWidth="6" strokeLinecap="round" />
        <path d="M40 70 C40 80 55 85 65 80" stroke="#F29111" strokeWidth="5" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("sqlite")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="25" width="70" height="50" rx="10" fill="#003B57" />
        <text x="50" y="58" textAnchor="middle" fill="#00DEFF" fontSize="24" fontFamily="monospace" fontWeight="bold">SQL</text>
      </svg>
    );
  }
  if (n.includes("snowflake")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 10 V90 M10 50 H90 M22 22 L78 78 M22 78 L78 22" stroke="#29B6F6" strokeWidth="6" strokeLinecap="round" />
        <circle cx="50" cy="50" r="10" fill="white" stroke="#29B6F6" strokeWidth="6" />
      </svg>
    );
  }
  if (n.includes("bigquery") || n.includes("gcs") || n.includes("google")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 15 L85 35 V75 L50 95 L15 75 V35 Z" stroke="#4285F4" strokeWidth="6" strokeLinejoin="round" />
        <path d="M50 15 V90" stroke="#EA4335" strokeWidth="4" />
        <path d="M15 35 H85" stroke="#FBBC05" strokeWidth="4" />
        <path d="M15 75 H85" stroke="#34A853" strokeWidth="4" />
      </svg>
    );
  }
  if (n.includes("databricks")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 15 L85 35 L50 55 L15 35 Z" fill="#FF3621" />
        <path d="M15 35 L50 55 V90 L15 70 Z" fill="#E21D0D" />
        <path d="M85 35 L50 55 V90 L85 70 Z" fill="#A81005" />
      </svg>
    );
  }
  if (n.includes("salesforce")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M25 65 C15 65 10 55 18 45 C15 30 30 20 45 25 C55 12 75 18 75 35 C88 35 92 50 82 60 C85 70 70 80 60 75 C50 82 30 80 25 65 Z" fill="#00A1E0" />
      </svg>
    );
  }
  if (n.includes("hubspot")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="14" fill="#FF7A59" />
        <path d="M50 50 L25 35 M50 50 L75 35 M50 50 L50 85" stroke="#FF7A59" strokeWidth="6" />
        <circle cx="25" cy="35" r="8" fill="#FF7A59" />
        <circle cx="75" cy="35" r="8" fill="#FF7A59" />
        <circle cx="50" cy="85" r="8" fill="#FF7A59" />
      </svg>
    );
  }
  if (n.includes("tableau")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 15 V85 M15 50 H85" stroke="#E15759" strokeWidth="8" strokeLinecap="round" />
        <path d="M32 32 L68 68 M32 68 L68 32" stroke="#4E79A7" strokeWidth="6" strokeLinecap="round" />
        <circle cx="50" cy="50" r="10" fill="#F28E2B" />
      </svg>
    );
  }
  if (n.includes("notion")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="12" fill="black" />
        <rect x="22" y="22" width="56" height="56" rx="6" fill="white" />
        <path d="M30 30 V70 M30 30 L55 58 M55 30 V70 M55 30 L30 30" stroke="black" strokeWidth="8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (n.includes("kafka")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="42" stroke="black" strokeWidth="8" />
        <circle cx="35" cy="35" r="10" fill="black" />
        <circle cx="65" cy="50" r="10" fill="black" />
        <circle cx="35" cy="65" r="10" fill="black" />
        <path d="M35 35 L65 50 L35 65" stroke="black" strokeWidth="6" />
      </svg>
    );
  }
  if (n.includes("mongodb")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 12 C50 12 25 35 25 55 C25 72 38 85 50 85 C62 85 75 72 75 55 C75 35 50 12 50 12 Z" fill="#13AA52" />
        <path d="M50 12 V85" stroke="#497424" strokeWidth="4" />
      </svg>
    );
  }
  if (n.includes("oracle")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <ellipse cx="50" cy="50" rx="42" ry="25" stroke="#F80000" strokeWidth="10" fill="none" />
      </svg>
    );
  }
  if (n.includes("s3")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 35 L50 20 L80 35 L50 50 Z" fill="#E07C24" />
        <path d="M20 35 V65 L50 80 V50 Z" fill="#A05010" />
        <path d="M80 35 V65 L50 80 V50 Z" fill="#CD6015" />
      </svg>
    );
  }
  if (n.includes("confluence")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 20 L50 35 L80 20 L50 80 Z" fill="#0052CC" />
        <path d="M20 20 L80 20 L50 80 Z" fill="#2684FF" fillOpacity="0.5" />
      </svg>
    );
  }
  if (n.includes("looker")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="30" cy="30" r="10" fill="#4285F4" />
        <circle cx="70" cy="30" r="10" fill="#EA4335" />
        <circle cx="50" cy="70" r="10" fill="#FBBC05" />
        <path d="M30 30 L50 70 L70 30" stroke="#34A853" strokeWidth="6" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("slack")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="35" cy="35" r="8" fill="#36C5F0" />
        <rect x="47" y="27" width="16" height="16" rx="8" fill="#2EB67D" />
        <rect x="27" y="47" width="16" height="16" rx="8" fill="#E01E5A" />
        <circle cx="65" cy="65" r="8" fill="#ECB22E" />
      </svg>
    );
  }
  if (n.includes("dbt")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M50 15 L85 35 V75 L50 95 L15 75 V35 Z" fill="#FF6B4A" />
        <text x="50" y="62" textAnchor="middle" fill="white" fontSize="32" fontFamily="sans-serif" fontWeight="bold">dbt</text>
      </svg>
    );
  }
  if (n.includes("airbyte")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="30" fill="#5F6368" />
        <circle cx="50" cy="45" r="18" fill="#6157FF" />
        <circle cx="44" cy="42" r="3" fill="white" />
        <circle cx="56" cy="42" r="3" fill="white" />
        <path d="M35 70 C35 75 45 75 45 60" stroke="#6157FF" strokeWidth="4" strokeLinecap="round" />
        <path d="M65 70 C65 75 55 75 55 60" stroke="#6157FF" strokeWidth="4" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("fivetran")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 50 H80 M50 20 L80 50 L50 80" stroke="#3E8DFF" strokeWidth="10" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M20 50 L40 30" stroke="#3E8DFF" strokeWidth="10" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("clickhouse")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="20" width="12" height="60" rx="3" fill="#F03" />
        <rect x="37" y="20" width="12" height="60" rx="3" fill="#F90" />
        <rect x="59" y="20" width="12" height="40" rx="3" fill="#0C0" />
        <rect x="81" y="20" width="12" height="60" rx="3" fill="#F03" />
      </svg>
    );
  }
  if (n.includes("sql server") || n === "mssql") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="12" fill="#E81123" fillOpacity="0.1" stroke="#E81123" strokeWidth="4" />
        <path d="M30 35 C30 28 40 25 50 25 C60 25 70 28 70 35 C70 42 60 45 50 45 C40 45 30 42 30 35 Z" fill="#E81123" />
        <path d="M30 35 V50 C30 57 40 60 50 60 C60 60 70 57 70 50 V35" stroke="#E81123" strokeWidth="5" />
        <path d="M30 50 V65 C30 72 40 75 50 75 C60 75 70 72 70 65 V50" stroke="#E81123" strokeWidth="5" />
      </svg>
    );
  }
  if (n.includes("duckdb")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="42" fill="#FFF200" fillOpacity="0.15" stroke="#FFF200" strokeWidth="4" />
        <path d="M35 55 C35 45 42 38 52 38 C62 38 68 45 68 55 C68 62 62 68 50 68 C40 68 35 62 35 55 Z" fill="#FFF200" />
        <circle cx="48" cy="48" r="3" fill="black" />
        <path d="M68 50 L78 54 L68 58" fill="#FF9F00" />
        <path d="M35 58 C30 58 28 65 35 65" stroke="black" strokeWidth="3" />
      </svg>
    );
  }
  if (n.includes("neo4j")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="30" r="10" fill="#008CC1" />
        <circle cx="30" cy="65" r="10" fill="#008CC1" />
        <circle cx="70" cy="65" r="10" fill="#008CC1" />
        <line x1="50" y1="30" x2="30" y2="65" stroke="#008CC1" strokeWidth="5" />
        <line x1="50" y1="30" x2="70" y2="65" stroke="#008CC1" strokeWidth="5" />
        <line x1="30" y1="65" x2="70" y2="65" stroke="#008CC1" strokeWidth="5" />
      </svg>
    );
  }
  if (n.includes("redis")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 30 L50 15 L80 30 L50 45 Z" fill="#D82C20" />
        <path d="M20 40 L50 55 L80 40" stroke="#A81D10" strokeWidth="5" strokeLinecap="round" />
        <path d="M20 40 V65 L50 80 V55 Z" fill="#A81D10" />
        <path d="M80 40 V65 L50 80 V55 Z" fill="#C52518" />
        <path d="M20 52 L50 67 L80 52" stroke="#8A1308" strokeWidth="5" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("jira")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M25 75 C25 65 35 55 50 55 L75 55 L50 80 L25 75 Z" fill="#0052CC" />
        <path d="M35 45 C35 35 45 25 60 25 L85 25 L60 50 L35 45 Z" fill="#2684FF" />
      </svg>
    );
  }
  if (n.includes("prometheus")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M20 70 C20 70 30 85 50 85 C70 85 80 70 80 70" stroke="#E6522C" strokeWidth="8" strokeLinecap="round" />
        <path d="M50 20 C50 20 30 45 40 65 C45 70 55 70 60 65 C70 45 50 20 50 20 Z" fill="#E6522C" />
      </svg>
    );
  }
  if (n.includes("grafana")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="38" stroke="#F89E1B" strokeWidth="6" />
        <path d="M35 65 C35 50 45 40 50 40 C55 40 65 50 65 65" stroke="#F89E1B" strokeWidth="6" strokeLinecap="round" />
        <path d="M50 40 V20" stroke="#F89E1B" strokeWidth="6" strokeLinecap="round" />
      </svg>
    );
  }
  if (n.includes("airflow")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="12" fill="#017A8A" />
        <path d="M50 38 C50 20 35 25 35 38 Z" fill="#017A8A" opacity="0.8" />
        <path d="M50 62 C50 80 65 75 65 62 Z" fill="#017A8A" opacity="0.8" />
        <path d="M62 50 C80 50 75 35 62 35 Z" fill="#017A8A" />
        <path d="M38 50 C20 50 25 65 38 65 Z" fill="#017A8A" />
      </svg>
    );
  }
  if (n.includes("cassandra")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="42" stroke="#1287B1" strokeWidth="4" />
        <ellipse cx="50" cy="50" rx="35" ry="15" stroke="#1287B1" strokeWidth="4" />
        <ellipse cx="50" cy="50" rx="15" ry="35" stroke="#1287B1" strokeWidth="4" />
      </svg>
    );
  }
  if (n.includes("dynamodb")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="20" y="20" width="25" height="25" rx="4" fill="#E07C24" />
        <rect x="55" y="20" width="25" height="25" rx="4" fill="#E07C24" />
        <rect x="20" y="55" width="25" height="25" rx="4" fill="#E07C24" />
        <rect x="55" y="55" width="25" height="25" rx="4" fill="#E07C24" />
        <path d="M45 32 H55 M32 45 V55 M68 45 V55 M45 68 H55" stroke="#CD6015" strokeWidth="4" />
      </svg>
    );
  }
  if (n.includes("powerbi")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="20" y="55" width="15" height="25" rx="3" fill="#E2A600" />
        <rect x="42" y="35" width="15" height="45" rx="3" fill="#F2C811" />
        <rect x="65" y="15" width="15" height="65" rx="3" fill="#FFEB3B" />
      </svg>
    );
  }
  if (n.includes("sharepoint")) {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="35" fill="#0078D4" />
        <circle cx="35" cy="50" r="10" fill="white" />
        <text x="35" y="56" textAnchor="middle" fill="#0078D4" fontSize="16" fontFamily="sans-serif" fontWeight="bold">S</text>
      </svg>
    );
  }

  const char = name.charAt(0).toUpperCase();
  const colors = [
    { bg: "bg-red-50 text-red-600 border-red-200" },
    { bg: "bg-blue-50 text-blue-600 border-blue-200" },
    { bg: "bg-green-50 text-green-600 border-green-200" },
    { bg: "bg-yellow-50 text-yellow-600 border-yellow-200" },
    { bg: "bg-purple-50 text-purple-600 border-purple-200" },
    { bg: "bg-pink-50 text-pink-600 border-pink-200" },
    { bg: "bg-indigo-50 text-indigo-600 border-indigo-200" },
    { bg: "bg-teal-50 text-teal-600 border-teal-200" },
    { bg: "bg-orange-50 text-orange-600 border-orange-200" }
  ];
  const hash = name.split("").reduce((acc, c) => acc + c.charCodeAt(0), 0);
  const color = colors[hash % colors.length];

  return (
    <div className={`w-10 h-10 rounded-xl flex items-center justify-center font-bold text-lg border ${color.bg}`}>
      {char}
    </div>
  );
}

function McpClientLogo({ id }: { id: string }) {
  if (id === "openclaw") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="50" cy="50" r="38" fill="#F39C12" fillOpacity="0.15" stroke="#F39C12" strokeWidth="4" />
        <path d="M30 65 C30 45 40 30 50 30 C60 30 70 45 70 65" stroke="#D35400" strokeWidth="6" strokeLinecap="round" />
        <path d="M40 30 L50 15 L60 30" stroke="#D35400" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="50" cy="50" r="8" fill="#E67E22" />
      </svg>
    );
  }
  if (id === "open_webui") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="18" fill="#16A085" fillOpacity="0.15" stroke="#16A085" strokeWidth="4" />
        <path d="M32 45 H68" stroke="#1abc9c" strokeWidth="6" strokeLinecap="round" />
        <path d="M32 60 H56" stroke="#1abc9c" strokeWidth="6" strokeLinecap="round" />
        <circle cx="70" cy="30" r="4" fill="#E74C3C" />
      </svg>
    );
  }
  if (id === "cursor_cline") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="18" fill="#8E44AD" fillOpacity="0.15" stroke="#8E44AD" strokeWidth="4" />
        <path d="M35 30 L65 50 L35 70 Z" fill="#9b59b6" />
        <path d="M60 45 L75 30" stroke="#8E44AD" strokeWidth="5" strokeLinecap="round" />
      </svg>
    );
  }
  if (id === "claude") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="18" fill="#D35400" fillOpacity="0.1" stroke="#D35400" strokeWidth="4" />
        <text x="50" y="65" textAnchor="middle" fill="#E67E22" fontSize="48" fontFamily="Georgia" fontWeight="bold">C</text>
      </svg>
    );
  }
  if (id === "ollama") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="18" fill="#34495E" fillOpacity="0.15" stroke="#34495E" strokeWidth="4" />
        <path d="M40 70 V40 C40 35 45 30 50 30 C55 30 60 35 60 40 V70" stroke="#2c3e50" strokeWidth="6" strokeLinecap="round" />
        <circle cx="45" cy="45" r="3" fill="#2c3e50" />
        <circle cx="55" cy="45" r="3" fill="#2c3e50" />
      </svg>
    );
  }
  if (id === "crewai") {
    return (
      <svg className="w-10 h-10" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="15" width="70" height="70" rx="18" fill="#27AE60" fillOpacity="0.15" stroke="#27AE60" strokeWidth="4" />
        <circle cx="35" cy="40" r="8" fill="#2ecc71" />
        <circle cx="65" cy="40" r="8" fill="#2ecc71" />
        <circle cx="50" cy="70" r="8" fill="#2ecc71" />
        <path d="M35 40 L50 70 L65 40" stroke="#27AE60" strokeWidth="4" />
      </svg>
    );
  }
  return <Sparkles size={24} className="text-indigo-500" />;
}

export default function ConnectorsPage() {
  const [items, setItems] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState(false);
  const [mode, setMode] = useState<"form" | "recipe">("form");
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [recipe, setRecipe] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState({ text: "", type: "" });
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [tables, setTables] = useState<Record<string, any[]>>({});
  const [browsing, setBrowsing] = useState<string | null>(null);
  
  const [guideMcp, setGuideMcp] = useState<McpClient | null>(null);
  const [guideKey, setGuideKey] = useState<string>("");
  const [guideKeyBusy, setGuideKeyBusy] = useState<boolean>(false);
  const [guideKeyErr, setGuideKeyErr] = useState<string>("");
  // what the generated key may do, and which transport the guide is showing
  const [guideRole, setGuideRole] = useState<"viewer" | "editor">("editor");
  const [guideMode, setGuideMode] = useState<"remote" | "local">("remote");

  const [selectedCategory, setSelectedCategory] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");

  const fetchItems = async () => {
    try { setItems((await contextV2Api.connectors())?.connectors || []); }
    catch (e) { console.error(e); }
    finally { setLoading(false); }
  };
  useEffect(() => { fetchItems(); }, []);

  const openAdd = () => { setForm({ ...EMPTY_FORM }); setRecipe(""); setMode("form"); setMsg({ text: "", type: "" }); setModal(true); };
  
  const openAddForCatalog = (connector: CatalogConnector) => {
    setForm({
      ...EMPTY_FORM,
      name: `${connector.name} Connection`,
      kind: connector.kind,
    });
    setRecipe("");
    setMode("form");
    setMsg({ text: "", type: "" });
    setModal(true);
  };

  const openEdit = (c: Connector) => {
    setForm({
      ...EMPTY_FORM, id: c.id, name: c.name,
      kind: c.kind || "postgres",
      host: c.host || "", port: c.port ? String(c.port) : "", database: c.database || "",
      username: c.username || "",
      optAccessKeyId: (c as any).options?.access_key_id || "",
      optRegion: (c as any).options?.region || "",
      authMethod: (c as any).options?.auth || "",
      accountName: (c as any).options?.account_name || "",
      tenantId: (c as any).options?.tenant_id || "",
      clientId: (c as any).options?.client_id || "",
      certPath: (c as any).options?.certificate_path || "",
      assumeRoleArn: (c as any).options?.assume_role_arn || "",
      chain: (c as any).options?.chain || "",
      lakeConnectorId: (c as any).lake_connector_id || "",
      directory: (c as any).directory || "",
      queryEngineId: (c as any).query_engine_id || "",
      secretProvider: (c.secret_provider as SecretProvider) || "inline",
    });
    setMode("form"); setMsg({ text: "", type: "" }); setModal(true);
  };

  const buildSpec = (): ConnectorSpec => {
    const fam = FAMILY(form.kind);
    if (fam === "format") {
      return {
        name: form.name, kind: form.kind,
        lake_connector_id: form.lakeConnectorId || undefined,
        directory: form.directory || undefined,
        query_engine_id: form.queryEngineId || undefined,
      };
    }
    if (fam === "lake") {
      const auth = form.authMethod || (form.kind === "s3" ? "access_key" : form.kind === "adls" ? "service_principal" : "hmac");
      const options: any = { auth };
      if (form.optAccessKeyId) options.access_key_id = form.optAccessKeyId;
      if (form.optRegion) options.region = form.optRegion;
      if (form.accountName) options.account_name = form.accountName;
      if (form.tenantId) options.tenant_id = form.tenantId;
      if (form.clientId) options.client_id = form.clientId;
      if (form.certPath) options.certificate_path = form.certPath;
      if (form.assumeRoleArn) options.assume_role_arn = form.assumeRoleArn;
      if (form.chain) options.chain = form.chain;
      const needsSecret = form.kind !== "local" && !(
        (form.kind === "s3" && auth === "credential_chain") ||
        (form.kind === "adls" && (auth === "managed_identity" || (auth === "service_principal" && form.certPath)))
      );
      let secret: SecretRef | undefined;
      if (needsSecret) {
        if (form.secretProvider === "inline" && form.secretValue) secret = { provider: "inline", value: form.secretValue };
        else if (form.secretProvider === "env" && form.secretVar) secret = { provider: "env", var: form.secretVar };
        else if (form.secretProvider === "azure_keyvault" && form.secretName)
          secret = { provider: "azure_keyvault", vault_url: form.vaultUrl || undefined, secret_name: form.secretName };
      }
      return { name: form.name, kind: form.kind, secret, options };
    }
    const uri = form.connectionString.trim();
    if (!ENGINE(form.kind).server) {
      return { name: form.name, kind: form.kind, connection_string: uri || undefined };
    }
    let secret: SecretRef | undefined;
    if (form.secretProvider === "inline" && form.secretValue) secret = { provider: "inline", value: form.secretValue };
    else if (form.secretProvider === "env" && form.secretVar) secret = { provider: "env", var: form.secretVar };
    else if (form.secretProvider === "azure_keyvault" && form.secretName)
      secret = { provider: "azure_keyvault", vault_url: form.vaultUrl || undefined, secret_name: form.secretName };
    return {
      name: form.name, kind: form.kind, host: form.host || undefined,
      port: form.port ? Number(form.port) : undefined, database: form.database || undefined,
      username: form.username || undefined, secret,
      connection_string: uri || undefined,
    };
  };

  const onSave = async () => {
    setBusy(true); setMsg({ text: "Saving…", type: "info" });
    try {
      if (mode === "recipe") {
        if (!recipe.trim()) { setMsg({ text: "Paste a YAML recipe.", type: "error" }); setBusy(false); return; }
        await contextV2Api.connectorFromRecipe(recipe, form.name || undefined);
      } else {
        if (!form.name.trim()) { setMsg({ text: "Name is required.", type: "error" }); setBusy(false); return; }
        const spec = buildSpec();
        if (form.id) await contextV2Api.updateConnector(form.id, spec);
        else await contextV2Api.createConnector(spec);
      }
      setModal(false);
      await fetchItems();
      setSelectedCategory("my_connections");
    } catch (e: any) {
      setMsg({ text: `Error: ${e?.response?.data?.detail || e?.message || e}`, type: "error" });
    } finally { setBusy(false); }
  };

  const onDelete = async (c: Connector) => {
    if (!confirm(`Delete connector "${c.name}"? Saved pipelines that reference it will need a new source.`)) return;
    try { await contextV2Api.deleteConnector(c.id); setItems((xs) => xs.filter((x) => x.id !== c.id)); }
    catch { alert("Failed to delete."); }
  };

  const onTest = async (c: Connector) => {
    setTesting(c.id); setTestResult((r) => ({ ...r, [c.id]: "" }));
    try {
      const res = await contextV2Api.testConnector(c.id);
      setTestResult((r) => ({ ...r, [c.id]: `ok:Connected — ${res.tables} tables` }));
    } catch (e: any) {
      setTestResult((r) => ({ ...r, [c.id]: `err:${e?.response?.data?.detail || e?.message || "failed"}` }));
    } finally { setTesting(null); }
  };

  const onBrowse = async (c: Connector) => {
    if (expanded === c.id) { setExpanded(null); return; }
    setExpanded(c.id);
    if (tables[c.id]) return;
    setBrowsing(c.id);
    try {
      const res = await contextV2Api.introspectSql({ connector_id: c.id });
      setTables((t) => ({ ...t, [c.id]: res?.tables || [] }));
    } catch (e: any) {
      setTables((t) => ({ ...t, [c.id]: [] }));
      setTestResult((r) => ({ ...r, [c.id]: `err:${e?.response?.data?.detail || "introspection failed"}` }));
    } finally { setBrowsing(null); }
  };

  const generateMcpKey = async (clientId: string) => {
    setGuideKeyBusy(true); setGuideKeyErr(""); setGuideKey("");
    try {
      // The role is a REAL boundary: the MCP server runs every call as this key, so a viewer key is
      // refused the write tools by the API itself.
      const res = await contextV2Api.createApiKey(clientId, guideRole);
      setGuideKey(res.key);
    } catch (e: any) {
      setGuideKeyErr(
        e?.response?.status === 403 
          ? "Only an admin can generate an agent key — sign in as admin."
          : (e?.response?.data?.detail || "Failed to generate key.")
      );
    } finally { setGuideKeyBusy(false); }
  };

  const providerBadge = (p?: string | null) =>
    p === "azure_keyvault" ? "Azure Key Vault" : p === "env" ? "Env var" : p === "inline" ? "Encrypted" : "—";

  const copyText = (t: string) => { try { navigator.clipboard?.writeText(t); } catch { /* noop */ } };

  // Filter Catalog Connectors based on selected Category and search query
  const filteredCatalog = CATALOG_CONNECTORS.filter((c) => {
    if (selectedCategory === "on_prem_connectors") {
      if (!c.onPrem) return false;
    } else if (selectedCategory !== "all" && selectedCategory !== "my_connections" && selectedCategory !== "mcp_clients") {
      const catObj = CATEGORIES.find((cat) => cat.id === selectedCategory);
      if (catObj && c.category !== catObj.name) return false;
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (
        c.name.toLowerCase().includes(q) ||
        c.category.toLowerCase().includes(q)
      );
    }
    return true;
  });

  return (
    <div className="max-w-7xl mx-auto pb-20 px-4">
      
      <div className="flex flex-col lg:flex-row gap-8 items-start">
        
        {/* Left Sidebar: Connector Categories */}
        <div className="w-full lg:w-64 shrink-0 bg-white border border-stone-200 rounded-2xl p-4 shadow-sm">
          <div className="space-y-6">
            
            {/* Active Connections Navigation */}
            <div>
              <h2 className="text-[11px] font-bold uppercase tracking-wider text-stone-400 mb-2.5 px-2">
                Manage
              </h2>
              <button
                onClick={() => { setSelectedCategory("my_connections"); setSearchQuery(""); }}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm text-left transition ${
                  selectedCategory === "my_connections"
                    ? "bg-indigo-50 text-indigo-900 font-semibold border-l-2 border-indigo-600"
                    : "text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                }`}
              >
                <Database size={16} className={selectedCategory === "my_connections" ? "text-indigo-600" : "text-stone-400"} />
                <span className="flex-1">My Connections</span>
                {items.length > 0 && (
                  <span className="bg-stone-100 text-stone-600 text-xs px-2 py-0.5 rounded-full font-medium">
                    {items.length}
                  </span>
                )}
              </button>
            </div>

            {/* Divider */}
            <div className="border-t border-stone-100"></div>

            {/* AI Agents (MCP) Section */}
            <div>
              <h2 className="text-[11px] font-bold uppercase tracking-wider text-stone-400 mb-2.5 px-2">
                AI Agents (MCP)
              </h2>
              <button
                onClick={() => { setSelectedCategory("mcp_clients"); setSearchQuery(""); }}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm text-left transition ${
                  selectedCategory === "mcp_clients"
                    ? "bg-indigo-50 text-indigo-900 font-semibold border-l-2 border-indigo-600"
                    : "text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                }`}
              >
                <Bot size={16} className={selectedCategory === "mcp_clients" ? "text-indigo-600" : "text-stone-400"} />
                <span className="flex-1">MCP Clients Hub</span>
                <span className="bg-emerald-100 text-emerald-800 text-[10px] px-1.5 py-0.5 rounded-full font-bold">
                  {MCP_CLIENTS.length}
                </span>
              </button>
            </div>

            {/* Divider */}
            <div className="border-t border-stone-100"></div>

            {/* On-Premises Section */}
            <div>
              <h2 className="text-[11px] font-bold uppercase tracking-wider text-stone-400 mb-2.5 px-2">
                On-Premises
              </h2>
              <button
                onClick={() => { setSelectedCategory("on_prem_connectors"); setSearchQuery(""); }}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm text-left transition ${
                  selectedCategory === "on_prem_connectors"
                    ? "bg-indigo-50 text-indigo-900 font-semibold border-l-2 border-indigo-600"
                    : "text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                }`}
              >
                <Terminal size={16} className={selectedCategory === "on_prem_connectors" ? "text-indigo-600" : "text-stone-400"} />
                <span className="flex-1">On-Premise Connectors</span>
                <span className="bg-amber-100 text-amber-800 text-[10px] px-1.5 py-0.5 rounded-full font-bold">
                  {CATALOG_CONNECTORS.filter(c => c.onPrem).length}
                </span>
              </button>
            </div>

            {/* Divider */}
            <div className="border-t border-stone-100"></div>

            {/* Connector Categories Navigation */}
            <div>
              <h2 className="text-[11px] font-bold uppercase tracking-wider text-stone-400 mb-2.5 px-2">
                Connector Categories
              </h2>
              <div className="space-y-1">
                {CATEGORIES.map((cat) => {
                  const isActive = selectedCategory === cat.id;
                  const count = cat.id === "all" 
                    ? CATALOG_CONNECTORS.length 
                    : CATALOG_CONNECTORS.filter((c) => c.category === cat.name).length;
                  
                  return (
                    <button
                      key={cat.id}
                      onClick={() => { setSelectedCategory(cat.id); setSearchQuery(""); }}
                      className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm text-left transition ${
                        isActive
                          ? "bg-indigo-50 text-indigo-900 font-semibold border-l-2 border-indigo-600"
                          : "text-stone-600 hover:bg-stone-50 hover:text-stone-900"
                      }`}
                    >
                      <span>{cat.name}</span>
                      {count > 0 && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                          isActive ? "bg-indigo-200/50 text-indigo-800 font-semibold" : "bg-stone-50 text-stone-500"
                        }`}>
                          {count}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>

          </div>
        </div>

        {/* Right Panel: Content View */}
        <div className="flex-1 min-w-0 w-full">
          
          {/* Search bar inside Catalog (except connections list) */}
          {selectedCategory !== "my_connections" && (
            <div className="relative mb-6">
              <span className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-stone-400">
                <Search size={18} />
              </span>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search connectors..."
                className="w-full bg-white border border-stone-200 rounded-xl pl-10 pr-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 placeholder-stone-400 shadow-sm transition"
              />
            </div>
          )}

          {/* Render Active Connections View */}
          {selectedCategory === "my_connections" ? (
            <div>
              {/* Header */}
              <PageHeader
                title="Connections"
                icon={Cable}
                description="Saved data sources. Credentials resolve from Azure Key Vault, env vars, or encrypted at rest — never stored in plaintext."
                actions={<Button variant="primary" icon={Plus} onClick={() => setSelectedCategory("all")}>Add Connector</Button>}
              />

              {/* Connections Telemetry Metrics Banner */}
              {!loading && items.length > 0 && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
                  <div className="bg-stone-50 border border-stone-200 rounded-2xl p-4 shadow-sm flex flex-col justify-between">
                    <span className="text-[10px] font-bold text-stone-400 uppercase tracking-wider">Total Connections</span>
                    <span className="text-2xl font-extrabold text-stone-900 mt-2">{items.length}</span>
                  </div>
                  <div className="bg-stone-50 border border-stone-200 rounded-2xl p-4 shadow-sm flex flex-col justify-between">
                    <span className="text-[10px] font-bold text-stone-400 uppercase tracking-wider">Active Connections</span>
                    <span className="text-2xl font-extrabold text-stone-900 mt-2">
                      {items.filter(c => testResult[c.id]?.startsWith("ok:")).length}
                    </span>
                  </div>
                </div>
              )}

              {loading ? (
                <div className="p-8 text-stone-500 animate-pulse">Loading connectors…</div>
              ) : items.length === 0 ? (
                <div className="bg-stone-50 border border-dashed border-stone-300 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
                  <Plug size={44} className="mb-4 text-stone-300" />
                  <h2 className="text-xl font-bold mb-2 text-stone-700">No connectors yet</h2>
                  <p className="max-w-md mb-6">Add any source — PostgreSQL, MySQL, SQLite, SQL Server, Oracle, or a data lake (AWS S3, Google Cloud Storage, Azure ADLS, or on-prem files). Once saved, it appears here.</p>
                  <Button variant="primary" icon={Plus} onClick={() => setSelectedCategory("all")}>Browse Connector Catalog</Button>
                </div>
              ) : (
                <div className="space-y-4">
                  {items.map((c) => {
                    const tr = testResult[c.id] || "";
                    const isOpen = expanded === c.id;
                    
                    let statusBadge = (
                      <span className="text-[10px] font-semibold text-stone-400 bg-stone-100 border border-stone-200 px-2 py-0.5 rounded-full flex items-center gap-1">
                        <HelpCircle size={10} /> Not Tested
                      </span>
                    );
                    if (tr.startsWith("ok:")) {
                      statusBadge = (
                        <span className="text-[10px] font-semibold text-emerald-600 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full flex items-center gap-1">
                          <CheckCircle2 size={10} /> Connected
                        </span>
                      );
                    } else if (tr.startsWith("err:")) {
                      statusBadge = (
                        <span className="text-[10px] font-semibold text-red-600 bg-red-50 border border-red-200 px-2 py-0.5 rounded-full flex items-center gap-1">
                          <AlertCircle size={10} /> Connection Failed
                        </span>
                      );
                    }

                    return (
                      <div key={c.id} className="bg-white border border-stone-200 rounded-3xl shadow-sm hover:shadow-md transition-shadow overflow-hidden">
                        <div className="p-5 flex flex-col md:flex-row md:items-center justify-between gap-4">
                          
                          {/* Left: Identity (Actual brand SVG logos) */}
                          <div className="flex items-start gap-4 min-w-0">
                            <div className="w-14 h-14 rounded-2xl bg-stone-50 border border-stone-100 flex items-center justify-center shrink-0 shadow-inner">
                              <ConnectorLogo name={getKindDisplayName(c.kind)} />
                            </div>
                            <div className="min-w-0">
                              <div className="flex items-center flex-wrap gap-2">
                                <h3 className="font-bold text-stone-900 text-lg truncate">{c.name}</h3>
                                <span className="text-[10px] font-extrabold uppercase tracking-wider px-2 py-0.5 rounded-full bg-stone-100 text-stone-600">
                                  {c.kind}
                                </span>
                                {statusBadge}
                              </div>
                              
                              <p className="text-xs text-stone-500 mt-1.5 truncate font-mono bg-stone-50 px-2.5 py-1 rounded-lg border border-stone-100 inline-block">
                                {(c as any).directory ? (c as any).directory : c.host ? `${c.host}${c.port ? ":" + c.port : ""}/${c.database || ""}` : (c.has_connection_string ? "connection string" : (["s3", "gcs", "adls", "local"].includes(c.kind) ? "data lake storage" : "—"))}
                                {c.schema ? ` · schema ${c.schema}` : ""}
                              </p>
                            </div>
                          </div>

                          {/* Right: Actions */}
                          <div className="flex items-center flex-wrap gap-2.5 shrink-0">
                            <span className="text-[11px] flex items-center gap-1.5 text-stone-500 bg-stone-50 border border-stone-200 px-2.5 py-1.5 rounded-xl font-medium">
                              <KeyRound size={12} className="text-indigo-500" /> 
                              {providerBadge(c.secret_provider)}
                            </span>
                            
                            <Button 
                              variant="ghost" 
                              size="sm" 
                              icon={ShieldCheck} 
                              busy={testing === c.id} 
                              onClick={() => onTest(c)}
                            >
                              Test Connection
                            </Button>
                            
                            <Button 
                              variant="ghost" 
                              size="sm" 
                              icon={Table2} 
                              onClick={() => onBrowse(c)}
                            >
                              Browse {isOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                            </Button>
                            
                            <div className="flex items-center gap-1 border-l border-stone-200 pl-2.5">
                              <button 
                                onClick={() => openEdit(c)} 
                                className="p-2 text-stone-400 hover:text-indigo-600 hover:bg-indigo-50 rounded-xl transition"
                                title="Edit Connection"
                              >
                                <Pencil size={16} />
                              </button>
                              <button 
                                onClick={() => onDelete(c)} 
                                className="p-2 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-xl transition"
                                title="Delete Connection"
                              >
                                <Trash2 size={16} />
                              </button>
                            </div>
                          </div>

                        </div>

                        {/* Error logs */}
                        {tr && tr.startsWith("err:") && (
                          <div className="px-5 pb-4 -mt-2 text-xs flex items-start gap-1.5 text-red-600 bg-red-50/20 py-3 border-t border-stone-100 font-mono">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" />
                            <div>
                              <span className="font-bold">Error detail:</span> {tr.slice(tr.indexOf(":") + 1)}
                            </div>
                          </div>
                        )}

                        {/* Collapsible database tables explorer */}
                        {isOpen && (
                          <div className="border-t border-stone-100 bg-stone-50/50 p-5">
                            {browsing === c.id ? (
                              <div className="text-xs text-stone-500 flex items-center gap-2">
                                <Loader2 size={14} className="animate-spin text-indigo-500" /> 
                                Introspecting schema and querying table structures…
                              </div>
                            ) : (tables[c.id] || []).length === 0 ? (
                              <p className="text-xs text-stone-400">No tables found (or connection failed — try Test Connection).</p>
                            ) : (
                              <div className="space-y-3">
                                <div className="text-xs font-bold text-stone-400 uppercase tracking-wider mb-2">
                                  Discovered Tables ({(tables[c.id] || []).length})
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3.5">
                                  {(tables[c.id] || []).map((t: any) => (
                                    <div key={t.name} className="bg-white border border-stone-200 rounded-2xl p-4 shadow-sm">
                                      <div className="flex items-center justify-between pb-2 border-b border-stone-100 mb-2">
                                        <span className="text-sm font-bold text-stone-850 flex items-center gap-1.5">
                                          <Table2 size={14} className="text-indigo-500" /> 
                                          {t.name}
                                        </span>
                                        <span className="text-[10px] font-bold text-stone-400 bg-stone-50 px-2 py-0.5 rounded-full border border-stone-100">
                                          {(t.columns || []).length} cols{typeof t.row_count === "number" ? ` · ${t.row_count} rows` : ""}
                                        </span>
                                      </div>
                                      <div className="flex flex-wrap gap-1 mt-2">
                                        {(t.columns || []).slice(0, 12).map((col: any) => (
                                          <span 
                                            key={col.name} 
                                            className={`text-[9px] px-1.5 py-0.5 rounded font-mono ${
                                              col.primary_key 
                                                ? "bg-amber-50 text-amber-700 border border-amber-200" 
                                                : col.foreign_key 
                                                  ? "bg-teal-50 text-teal-700 border border-teal-200" 
                                                  : "bg-stone-100 text-stone-500"
                                            }`}
                                          >
                                            {col.name}
                                          </span>
                                        ))}
                                        {(t.columns || []).length > 12 && (
                                          <span className="text-[9px] text-stone-400 font-bold bg-stone-50 px-1.5 py-0.5 rounded border border-stone-100">
                                            +{(t.columns || []).length - 12} more
                                          </span>
                                        )}
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ) : selectedCategory === "mcp_clients" ? (
            
            /* Render MCP Clients Hub */
            <div>
              <div className="mb-6">
                <h1 className="text-2xl font-bold tracking-tight text-stone-900 flex items-center gap-2">
                  <Bot className="text-indigo-600" size={26} />
                  MCP Integration Hub
                </h1>
                <p className="text-sm text-stone-500 mt-1">
                  Connect autonomous agents, frameworks, and chat UIs to this workspace using the Model Context Protocol (MCP).
                  Pick your client below for step-by-step setup — or read the essentials first.
                </p>
              </div>

              {/* How connecting works — the three things every client needs, before the per-client guides */}
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-8">
                <div className="bg-white border border-stone-200 rounded-2xl p-5 space-y-2">
                  <div className="flex items-center gap-2 text-xs font-bold text-stone-500 uppercase tracking-wider">
                    <Cable size={13} className="text-indigo-600" /> 1 · The endpoint
                  </div>
                  <code className="block bg-stone-900 text-emerald-300 text-[11px] font-mono px-3 py-2 rounded-lg overflow-x-auto">
                    {MCP_ENDPOINT}
                  </code>
                  <p className="text-xs text-stone-600 leading-relaxed">
                    Streamable-HTTP MCP. Health check at <span className="font-mono">/healthz</span>.
                    Older <span className="font-mono">/sse</span> paths are no longer served.
                  </p>
                </div>

                <div className="bg-white border border-stone-200 rounded-2xl p-5 space-y-2">
                  <div className="flex items-center gap-2 text-xs font-bold text-stone-500 uppercase tracking-wider">
                    <KeyRound size={13} className="text-indigo-600" /> 2 · A key per agent
                  </div>
                  <p className="text-xs text-stone-600 leading-relaxed">
                    Every HTTP request must carry <span className="font-mono">X-API-Key</span> — the server
                    refuses unauthenticated calls, including tool discovery. Generate a key inside any
                    client guide below, or in <span className="font-semibold">Users &amp; Access</span>.
                  </p>
                  <p className="text-xs text-stone-600 leading-relaxed">
                    Each call runs <span className="font-semibold">as that key</span>: a
                    <span className="font-mono"> viewer</span> key can read but is refused every write tool;
                    <span className="font-mono"> editor</span> can ingest and write memory. Revoking a key cuts
                    that agent off within a minute, and nothing else.
                  </p>
                </div>

                <div className="bg-white border border-stone-200 rounded-2xl p-5 space-y-2">
                  <div className="flex items-center gap-2 text-xs font-bold text-stone-500 uppercase tracking-wider">
                    <ShieldCheck size={13} className="text-indigo-600" /> 3 · Local or remote
                  </div>
                  <p className="text-xs text-stone-600 leading-relaxed">
                    <span className="font-semibold">Local</span> — the port is published on
                    <span className="font-mono"> 127.0.0.1</span> only, so anything on this machine can connect
                    with a key. Clients that can&apos;t speak HTTP (Claude Desktop) launch the server over stdio instead.
                  </p>
                  <p className="text-xs text-stone-600 leading-relaxed">
                    <span className="font-semibold">Remote</span> — put a reverse proxy in front, terminate
                    TLS there, and publish that hostname instead of widening the port. Keys travel in a header:
                    never send them over plain HTTP off-host.
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {MCP_CLIENTS.filter(c => searchQuery === "" || c.name.toLowerCase().includes(searchQuery.toLowerCase())).map((c) => (
                  <button
                    key={c.id}
                    onClick={() => { setGuideMcp(c); setGuideKey(""); setGuideKeyErr("");
                      setGuideMode((c.supports || ["remote"])[0]); setGuideRole("editor"); }}
                    className="bg-white border border-stone-200 rounded-3xl p-6 flex flex-col gap-4 shadow-sm hover:border-indigo-500 hover:shadow-lg transition text-left group w-full relative overflow-hidden"
                  >
                    <div className="flex items-center justify-between">
                      <div className="w-14 h-14 rounded-2xl bg-stone-50 border border-stone-100 flex items-center justify-center shadow-inner group-hover:scale-105 transition-transform">
                        <McpClientLogo id={c.id} />
                      </div>
                      <span className="text-xs font-semibold text-indigo-600 bg-indigo-50 px-3 py-1.5 rounded-full group-hover:bg-indigo-600 group-hover:text-white transition-colors">
                        Configure
                      </span>
                    </div>

                    <div>
                      <h3 className="font-bold text-stone-900 text-lg truncate group-hover:text-indigo-600 transition-colors">
                        {c.name}
                      </h3>
                      <p className="text-xs text-stone-500 font-semibold mt-1">
                        {c.subtitle}
                      </p>
                    </div>

                    <p className="text-xs text-stone-600 leading-relaxed line-clamp-2">
                      {c.description}
                    </p>
                  </button>
                ))}
              </div>
            </div>

          ) : (
            
            /* Render Catalog View */
            <div>
              <div className="text-sm text-stone-500 mb-4">
                {selectedCategory === "on_prem_connectors" ? "On-premises connectors" : `${filteredCatalog.length} connector${filteredCatalog.length !== 1 ? "s" : ""}`}
              </div>

              {filteredCatalog.length === 0 ? (
                <div className="bg-stone-50 border border-dashed border-stone-200 rounded-2xl p-12 text-center text-stone-500">
                  <p className="text-base">No connectors match your search filter.</p>
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  {filteredCatalog.map((c) => (
                    <button
                      key={c.id}
                      onClick={() => openAddForCatalog(c)}
                      className="bg-white border border-stone-200 rounded-3xl p-6 flex flex-col gap-4 shadow-sm hover:border-indigo-500 hover:shadow-lg transition text-left group w-full relative overflow-hidden"
                    >
                      <div className="flex items-center justify-between w-full">
                        <div className="w-14 h-14 rounded-2xl bg-stone-50 border border-stone-100 flex items-center justify-center shadow-inner group-hover:scale-105 transition-transform">
                          <ConnectorLogo name={c.name} />
                        </div>
                        <span className="text-xs font-semibold text-indigo-600 bg-indigo-50 px-3 py-1.5 rounded-full group-hover:bg-indigo-600 group-hover:text-white transition-colors">
                          Connect
                        </span>
                      </div>

                      <div>
                        <h3 className="font-bold text-stone-900 text-lg truncate group-hover:text-indigo-600 transition-colors">
                          {c.name}
                        </h3>
                        <p className="text-xs text-stone-500 font-semibold mt-1">
                          {c.category}
                        </p>
                      </div>

                      <p className="text-xs text-stone-600 leading-relaxed line-clamp-2">
                        {getConnectorDescription(c)}
                      </p>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

        </div>

      </div>

      {/* Setup Guide Modal for MCP Clients */}
      {guideMcp && (() => {
        // Which snippets this guide is showing right now. A client that only supports one transport
        // always shows that one, whatever the toggle last was.
        const modes = guideMcp.supports || ["remote"];
        const mode = modes.includes(guideMode) ? guideMode : modes[0];
        const isLocalGuide = mode === "local";
        const guideSteps = (isLocalGuide && guideMcp.localSteps) || guideMcp.setupSteps;
        const guideConfig = isLocalGuide ? guideMcp.localConfigJson : guideMcp.configJson;
        const guideCmd = isLocalGuide ? guideMcp.localCmdBlock : guideMcp.cmdBlock;
        const withKey = (s: string) => s.replace(/YOUR_API_KEY_HERE/g, guideKey || "YOUR_API_KEY_HERE");
        return (
        <div className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[70] flex items-center justify-center p-4" onClick={() => setGuideMcp(null)}>
          <div className="bg-white rounded-3xl shadow-2xl w-full max-w-2xl max-h-[92vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            
            {/* Header */}
            <div className="px-6 py-5 border-b border-stone-100 flex items-center justify-between sticky top-0 bg-white z-10">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-stone-50 border border-stone-100 flex items-center justify-center">
                  <McpClientLogo id={guideMcp.id} />
                </div>
                <div>
                  <h2 className="font-bold text-stone-900 text-lg">{guideMcp.name} Integration</h2>
                  <p className="text-xs text-stone-500 font-semibold">{guideMcp.subtitle}</p>
                </div>
              </div>
              <button onClick={() => setGuideMcp(null)} className="p-1.5 text-stone-400 hover:bg-stone-100 rounded-lg"><X size={18} /></button>
            </div>

            {/* Content */}
            <div className="p-6 space-y-6">
              
              {/* Description */}
              <div className="space-y-2">
                <p className="text-sm text-stone-700 leading-relaxed">{guideMcp.description}</p>
                <div className="p-3.5 bg-indigo-50/50 border border-indigo-100/50 rounded-xl text-xs text-indigo-950 leading-relaxed">
                  <span className="font-bold block text-indigo-900 mb-0.5">Unified Integration Benefit</span>
                  {guideMcp.whyInDemand}
                </div>
              </div>

              {/* API Key Generation */}
              <div className="bg-stone-50 border border-stone-200 rounded-2xl p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5 text-xs font-bold text-stone-600 uppercase tracking-wider">
                    <KeyRound size={13} className="text-indigo-600" />
                    Workspace API Authentication
                  </div>
                  {guideKey && (
                    <span className="text-[10px] font-bold bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded-full">
                      Key Active
                    </span>
                  )}
                </div>
                <p className="text-xs text-stone-500">
                  Generate a key for {guideMcp.name}. It is shown <span className="font-semibold">once</span> — copy it now;
                  we store only a hash. It is substituted into the snippets below automatically.
                </p>

                {/* Role = what this agent may do. The server enforces it on every call, so this is a
                    real permission boundary, not a label. */}
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-bold text-stone-500 uppercase tracking-wider">Role</span>
                  {([
                    ["viewer", "Read-only — search, answer, browse. Every write tool is refused."],
                    ["editor", "Read + write — can ingest documents, write memory, propose changes."],
                  ] as const).map(([role, help]) => (
                    <button
                      key={role}
                      title={help}
                      onClick={() => setGuideRole(role)}
                      className={`px-2.5 py-1 rounded-lg text-[11px] font-bold border transition-colors ${
                        guideRole === role
                          ? "bg-indigo-600 text-white border-indigo-600"
                          : "bg-white text-stone-600 border-stone-200 hover:bg-stone-50"
                      }`}
                    >
                      {role}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-stone-500 leading-relaxed">
                  {guideRole === "viewer"
                    ? "Read-only: search, answer, browse the catalogue and graph. Ingestion, memory writes and proposals are refused by the server."
                    : "Read + write: can also ingest documents, run ingestions, write memory and raise proposals. Give this only to agents that need it."}
                </p>

                {guideKeyErr && (
                  <div className="text-xs text-red-600 font-medium flex items-center gap-1">
                    <AlertCircle size={12} /> {guideKeyErr}
                  </div>
                )}

                <div className="flex gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    busy={guideKeyBusy}
                    onClick={() => generateMcpKey(guideMcp.id)}
                  >
                    {guideKey ? "Generate another key" : `Generate ${guideRole} key`}
                  </Button>
                  {guideKey && (
                    <div className="flex-1 flex items-center bg-white border border-stone-200 rounded-lg px-3 py-1 font-mono text-[11px] text-stone-700 select-all overflow-x-auto truncate">
                      {guideKey}
                    </div>
                  )}
                  {guideKey && (
                    <button onClick={() => copyText(guideKey)} title="Copy key"
                      className="px-2 text-stone-400 hover:text-indigo-600"><Copy size={14} /></button>
                  )}
                </div>
                {guideKey && (
                  <p className="text-[11px] text-emerald-700 font-medium">
                    Key created. Revoke it any time in Users &amp; Access — the agent loses access within a minute.
                  </p>
                )}
              </div>

              {/* Local vs remote — only shown when this client actually supports both */}
              {(guideMcp.supports || ["remote"]).length > 1 && (
                <div className="flex items-center gap-1.5 bg-stone-100 p-1 rounded-xl w-fit">
                  {(guideMcp.supports || ["remote"]).map((mode) => (
                    <button
                      key={mode}
                      onClick={() => setGuideMode(mode)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-colors ${
                        guideMode === mode ? "bg-white text-indigo-600 shadow-sm" : "text-stone-500 hover:text-stone-800"
                      }`}
                    >
                      {mode === "remote" ? "Remote (HTTP)" : "Local (stdio)"}
                    </button>
                  ))}
                </div>
              )}

              {guideMcp.note && (
                <div className="p-3.5 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-900 leading-relaxed flex gap-2">
                  <AlertCircle size={14} className="shrink-0 mt-0.5 text-amber-600" />
                  <span>{guideMcp.note}</span>
                </div>
              )}

              {/* Setup Steps — local mode has its own when the flow genuinely differs */}
              <div className="space-y-3">
                <h3 className="text-xs font-bold text-stone-500 uppercase tracking-wider">
                  Setup Instructions {isLocalGuide && "· local (stdio)"}
                </h3>
                <ol className="space-y-2.5 text-xs text-stone-600 leading-relaxed list-decimal pl-4">
                  {guideSteps.map((step, idx) => (
                    <li key={idx}>{step}</li>
                  ))}
                </ol>
              </div>

              {/* Config Files */}
              {guideConfig && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs font-bold text-stone-500 uppercase tracking-wider">
                    <span>mcpServers JSON Config</span>
                    <button
                      onClick={() => copyText(withKey(guideConfig))}
                      className="text-primary hover:underline flex items-center gap-1 font-semibold"
                    >
                      <Copy size={11} /> Copy Code
                    </button>
                  </div>
                  <pre className="bg-stone-900 text-emerald-300 text-[11px] font-mono p-4 rounded-2xl overflow-x-auto whitespace-pre">
                    {withKey(guideConfig)}
                  </pre>
                  {!guideKey && guideConfig.includes("YOUR_API_KEY_HERE") && (
                    <p className="text-[11px] text-amber-700">
                      Generate a key above and this snippet fills itself in — the server rejects the placeholder.
                    </p>
                  )}
                </div>
              )}

              {/* Commands */}
              {guideCmd && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs font-bold text-stone-500 uppercase tracking-wider">
                    <span>CLI Action Command</span>
                    <button
                      onClick={() => copyText(withKey(guideCmd))}
                      className="text-primary hover:underline flex items-center gap-1 font-semibold"
                    >
                      <Copy size={11} /> Copy CLI
                    </button>
                  </div>
                  <pre className="bg-stone-900 text-emerald-300 text-[11px] font-mono p-4 rounded-2xl overflow-x-auto whitespace-pre-wrap break-all">
                    {withKey(guideCmd)}
                  </pre>
                </div>
              )}

              {/* Verify + troubleshoot — what "it works" looks like, and the three real failure modes */}
              <div className="space-y-2">
                <h3 className="text-xs font-bold text-stone-500 uppercase tracking-wider">Verify the connection</h3>
                <pre className="bg-stone-900 text-emerald-300 text-[11px] font-mono p-4 rounded-2xl overflow-x-auto whitespace-pre-wrap break-all">
{`curl ${MCP_ENDPOINT.replace("/mcp", "/healthz")}
# {"status":"ok","auth":"required"}`}
                </pre>
                <ul className="text-[11px] text-stone-600 leading-relaxed space-y-1 list-disc pl-4">
                  <li><span className="font-mono font-semibold">401</span> — missing, wrong or revoked key. Generate a new one above.</li>
                  <li><span className="font-mono font-semibold">403</span> on a tool — the key is valid but its role may not do that (viewer calling a write tool).</li>
                  <li><span className="font-mono font-semibold">503</span> — the key could not be checked because the OpenCortyx API is down. Not a key problem; retry.</li>
                  <li><span className="font-mono font-semibold">429</span> — too many failed attempts from this address; wait a minute.</li>
                </ul>
              </div>

            </div>

            {/* Footer */}
            <div className="px-6 py-4 border-t border-stone-100 flex justify-end sticky bottom-0 bg-white">
              <Button variant="secondary" onClick={() => setGuideMcp(null)}>Close Guide</Button>
            </div>

          </div>
        </div>
        );
      })()}

      {/* Add / Edit modal */}
      {modal && (
        <div className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4" onClick={() => !busy && setModal(false)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[92vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between sticky top-0 bg-white z-10">
              <h2 className="font-bold text-stone-900 flex items-center gap-2"><Cable size={18} className="text-teal-500" /> {form.id ? "Edit" : "Add"} Connector</h2>
              <button onClick={() => setModal(false)} className="p-1.5 text-stone-400 hover:bg-stone-100 rounded-md"><X size={18} /></button>
            </div>

            {!form.id && (
              <div className="px-6 pt-4">
                <div className="inline-flex bg-stone-100 rounded-lg p-1 text-sm">
                  <button onClick={() => setMode("form")} className={`px-4 py-1.5 rounded-md font-medium ${mode === "form" ? "bg-white shadow-sm text-stone-900" : "text-stone-500"}`}>Form</button>
                  <button onClick={() => setMode("recipe")} className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${mode === "recipe" ? "bg-white shadow-sm text-stone-900" : "text-stone-500"}`}><FileCode size={14} /> YAML recipe</button>
                </div>
              </div>
            )}

            <div className="p-6 space-y-4">
              {mode === "recipe" ? (
                <>
                  <Field label="Name (optional)"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className={inputCls} placeholder="My source" /></Field>
                  <Field label="DataHub-style recipe (YAML)">
                    <textarea value={recipe} onChange={(e) => setRecipe(e.target.value)} spellCheck={false}
                       className="w-full h-56 bg-stone-900 text-emerald-300 font-mono text-xs p-3 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      placeholder={"source:\n  type: postgres\n  config:\n    host_port: db.company.com:5432\n    database: sales\n    username: reader\n    password: ${DB_PASSWORD}"} />
                  </Field>
                  <p className="text-xs text-stone-400">A <code>${"{VAR}"}</code> password becomes an env‑var reference; a literal is encrypted at rest.</p>
                </>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className={inputCls} placeholder="Sales DB" /></Field>
                    <Field label="Type">
                      <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })} className={inputCls}>
                        {ENGINES.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
                      </select>
                    </Field>
                  </div>

                  {FAMILY(form.kind) === "format" ? (
                    <>
                      <Field label="Data lake (storage)">
                        <select value={form.lakeConnectorId} onChange={(e) => setForm({ ...form, lakeConnectorId: e.target.value })} className={inputCls}>
                          <option value="">Select a data-lake connector…</option>
                          {items.filter((x) => ["s3", "gcs", "adls", "local"].includes(x.kind)).map((x) => <option key={x.id} value={x.id}>{x.name} ({x.kind})</option>)}
                        </select>
                        {items.filter((x) => ["s3", "gcs", "adls", "local"].includes(x.kind)).length === 0 && <p className="text-xs text-amber-600 mt-1.5">No data-lake connectors yet — add an S3 / GCS / ADLS / Local one first.</p>}
                      </Field>
                      <Field label={form.kind === "files" ? "Directory (holds the files)" : "Directory (holds the tables)"}>
                        <input value={form.directory} onChange={(e) => setForm({ ...form, directory: e.target.value })} className={inputCls + " font-mono text-xs"} placeholder={form.kind === "files" ? "s3://bucket/docs/  (PDFs, docx, txt — read recursively)" : "s3://bucket/warehouse/  (each sub-folder = one table)"} />
                      </Field>
                      {form.kind !== "files" && (
                        <Field label="Query engine">
                          <select value={form.queryEngineId} onChange={(e) => setForm({ ...form, queryEngineId: e.target.value })} className={inputCls}>
                            <option value="">Select a query engine…</option>
                            {items.filter((x) => x.kind === "duckdb").map((x) => <option key={x.id} value={x.id}>{x.name} (duckdb)</option>)}
                          </select>
                          {items.filter((x) => x.kind === "duckdb").length === 0 && <p className="text-xs text-amber-600 mt-1.5">No query engine yet — add a DuckDB connector first.</p>}
                        </Field>
                      )}
                      <p className="text-xs text-stone-400">{form.kind === "files"
                        ? "Reads PDF/docx/txt/… files from the directory (recursive) and ingests each as a document (parse → chunk → extract). Pick a document pipeline when you create the ingestion."
                        : `DuckDB scans each table folder under the directory (${form.kind === "delta" ? "delta_scan" : form.kind === "iceberg" ? "iceberg_scan" : "parquet base files"}). You pick the tables when you create an ingestion.`}</p>
                    </>
                  ) : FAMILY(form.kind) === "lake" ? (
                    (() => {
                      const auth = form.authMethod || (form.kind === "s3" ? "access_key" : form.kind === "adls" ? "service_principal" : "hmac");
                      const showSecret = form.kind !== "local" && !(
                        (form.kind === "s3" && auth === "credential_chain") ||
                        (form.kind === "adls" && (auth === "managed_identity" || (auth === "service_principal" && form.certPath)))
                      );
                      const secLabel = form.kind === "adls" ? (auth === "sas" ? "SAS / connection string" : "Client secret") : form.kind === "gcs" ? "HMAC secret" : "Secret access key";
                      return (
                        <>
                          {form.kind === "local" && (
                            <p className="text-xs text-stone-500">On-prem / local files — no credentials. A table-format connector points at an absolute directory reachable inside the container.</p>
                          )}
                          {form.kind === "s3" && (
                            <>
                              <div className="grid grid-cols-2 gap-3">
                                <Field label="Auth method"><select value={auth} onChange={(e) => setForm({ ...form, authMethod: e.target.value })} className={inputCls}><option value="access_key">Access key + secret</option><option value="credential_chain">IAM role / STS (credential chain)</option></select></Field>
                                <Field label="Region"><input value={form.optRegion} onChange={(e) => setForm({ ...form, optRegion: e.target.value })} className={inputCls} placeholder="us-east-1" /></Field>
                              </div>
                              {auth === "access_key" ? (
                                <Field label="Access key ID"><input value={form.optAccessKeyId} onChange={(e) => setForm({ ...form, optAccessKeyId: e.target.value })} className={inputCls} placeholder="AKIA…" /></Field>
                              ) : (
                                <>
                                  <Field label="AssumeRole ARN (optional)"><input value={form.assumeRoleArn} onChange={(e) => setForm({ ...form, assumeRoleArn: e.target.value })} className={inputCls + " font-mono text-xs"} placeholder="arn:aws:iam::123:role/data-reader" /></Field>
                                  <p className="text-xs text-amber-600">Uses the API container&apos;s AWS credential chain (env / instance profile / STS). No key stored.</p>
                                </>
                              )}
                            </>
                          )}
                          {form.kind === "gcs" && (
                            <>
                              <Field label="HMAC access key ID"><input value={form.optAccessKeyId} onChange={(e) => setForm({ ...form, optAccessKeyId: e.target.value })} className={inputCls} placeholder="GOOG1E…" /></Field>
                              <p className="text-xs text-amber-600">GCP: DuckDB uses an <b>HMAC interoperability key</b> (mint one for your service account). Service-account JSON / Workload Identity isn&apos;t supported by DuckDB&apos;s GCS reader.</p>
                            </>
                          )}
                          {form.kind === "adls" && (
                            <>
                              <div className="grid grid-cols-2 gap-3">
                                <Field label="Auth method"><select value={auth} onChange={(e) => setForm({ ...form, authMethod: e.target.value })} className={inputCls}><option value="service_principal">Service principal</option><option value="sas">SAS / connection string</option><option value="managed_identity">Managed identity</option></select></Field>
                                <Field label="Storage account"><input value={form.accountName} onChange={(e) => setForm({ ...form, accountName: e.target.value })} className={inputCls} placeholder="mystorageacct" /></Field>
                              </div>
                              {auth === "service_principal" && (
                                <>
                                  <div className="grid grid-cols-2 gap-3">
                                    <Field label="Tenant ID"><input value={form.tenantId} onChange={(e) => setForm({ ...form, tenantId: e.target.value })} className={inputCls} placeholder="tenant guid" /></Field>
                                    <Field label="Client ID"><input value={form.clientId} onChange={(e) => setForm({ ...form, clientId: e.target.value })} className={inputCls} placeholder="app / client guid" /></Field>
                                  </div>
                                  <Field label="Certificate path (optional — blank = use a secret)"><input value={form.certPath} onChange={(e) => setForm({ ...form, certPath: e.target.value })} className={inputCls + " font-mono text-xs"} placeholder="/app/secrets/sp-cert.pem (in container)" /></Field>
                                </>
                              )}
                              {auth === "managed_identity" && <p className="text-xs text-amber-600">Uses the container&apos;s managed identity (DefaultAzureCredential). No secret stored.</p>}
                            </>
                          )}
                          {showSecret && (
                            <div className="border border-stone-200 rounded-lg p-3 bg-stone-50/50">
                              <div className="flex items-center gap-2 mb-2"><KeyRound size={14} className="text-stone-500" /><span className="text-xs font-bold text-stone-600 uppercase tracking-wider">{secLabel}</span></div>
                              <select value={form.secretProvider} onChange={(e) => setForm({ ...form, secretProvider: e.target.value as SecretProvider })} className={inputCls + " mb-2"}>
                                <option value="inline">Inline (encrypted at rest)</option>
                                <option value="azure_keyvault">Azure Key Vault</option>
                                <option value="env">Environment variable</option>
                              </select>
                              {form.secretProvider === "inline" && <input type="password" value={form.secretValue} onChange={(e) => setForm({ ...form, secretValue: e.target.value })} className={inputCls} placeholder={form.id ? "•••••• (leave blank to keep)" : (form.kind === "adls" && auth === "sas" ? "BlobEndpoint=…;SharedAccessSignature=…" : "secret")} />}
                              {form.secretProvider === "env" && <input value={form.secretVar} onChange={(e) => setForm({ ...form, secretVar: e.target.value })} className={inputCls} placeholder="LAKE_SECRET" />}
                              {form.secretProvider === "azure_keyvault" && <div className="space-y-2"><input value={form.vaultUrl} onChange={(e) => setForm({ ...form, vaultUrl: e.target.value })} className={inputCls} placeholder="https://my-vault.vault.azure.net (optional)" /><input value={form.secretName} onChange={(e) => setForm({ ...form, secretName: e.target.value })} className={inputCls} placeholder="secret name" /></div>}
                            </div>
                          )}
                          <p className="text-xs text-stone-400">A storage connector holds credentials only. Create a Delta / Iceberg / Hudi connector to read a directory of tables from this lake.</p>
                        </>
                      );
                    })()
                  ) : !ENGINE(form.kind).server ? (
                    <Field label="Connection URI">
                      <input value={form.connectionString} onChange={(e) => setForm({ ...form, connectionString: e.target.value })} className={inputCls + " font-mono text-xs"} placeholder={ENGINE(form.kind).uri} />
                      <p className="text-xs text-stone-400 mt-1.5">
                        One URI carries everything for <b>{ENGINE(form.kind).label}</b> (host, auth, db). Example: <code className="break-all">{ENGINE(form.kind).uri}</code>.
                        {form.kind === "sqlite" && <><br /><span className="text-amber-600">Docker:</span> use the in‑container path (e.g. <code>/app/spider2-databases/chinook.sqlite</code>), not your host path.</>}
                        {form.kind === "duckdb" && <><br />DuckDB reads <b>Parquet/CSV/Iceberg/Delta</b> files — point it at a .duckdb file (or attach files in queries).</>}
                        {!["sqlite", "duckdb", "postgres", "mysql"].includes(form.kind) && <><br /><span className="text-amber-600">Driver:</span> install the dialect&apos;s Python driver in the API image if it isn&apos;t bundled — Test will tell you if it&apos;s missing.</>}
                      </p>
                    </Field>
                  ) : (
                    <>
                      <div className="grid grid-cols-3 gap-3">
                        <div className="col-span-2"><Field label="Host"><input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} className={inputCls} placeholder="db.company.com" /></Field></div>
                        <Field label="Port"><input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} className={inputCls} placeholder={form.kind === "mysql" ? "3306" : "5432"} /></Field>
                      </div>
                      <div className="grid grid-cols-2 gap-3">
                        <Field label="Database"><input value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} className={inputCls} placeholder="sales" /></Field>
                        <Field label="Username"><input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} className={inputCls} placeholder="reader" /></Field>
                      </div>
                      <p className="text-xs text-stone-400 -mt-1">Connection only — you&apos;ll pick schema, tables & views when you create an ingestion.</p>

                      {/* secret */}
                      <div className="border border-stone-200 rounded-lg p-3 bg-stone-50/50">
                        <div className="flex items-center gap-2 mb-2"><KeyRound size={14} className="text-stone-500" /><span className="text-xs font-bold text-stone-600 uppercase tracking-wider">Password / Secret</span></div>
                        <select value={form.secretProvider} onChange={(e) => setForm({ ...form, secretProvider: e.target.value as SecretProvider })} className={inputCls + " mb-2"}>
                          <option value="inline">Inline (encrypted at rest)</option>
                          <option value="azure_keyvault">Azure Key Vault</option>
                          <option value="env">Environment variable</option>
                        </select>
                        {form.secretProvider === "inline" && (
                          <input type="password" value={form.secretValue} onChange={(e) => setForm({ ...form, secretValue: e.target.value })} className={inputCls} placeholder={form.id ? "•••••• (leave blank to keep)" : "password"} />
                        )}
                        {form.secretProvider === "env" && (
                          <input value={form.secretVar} onChange={(e) => setForm({ ...form, secretVar: e.target.value })} className={inputCls} placeholder="DB_PASSWORD" />
                        )}
                        {form.secretProvider === "azure_keyvault" && (
                          <div className="space-y-2">
                            <input value={form.vaultUrl} onChange={(e) => setForm({ ...form, vaultUrl: e.target.value })} className={inputCls} placeholder="https://my-vault.vault.azure.net (optional → default)" />
                            <input value={form.secretName} onChange={(e) => setForm({ ...form, secretName: e.target.value })} className={inputCls} placeholder="secret name, e.g. sales-db-password" />
                          </div>
                        )}
                      </div>

                      {/* optional raw URI override */}
                      <Field label="Or paste a full connection URI (optional)">
                        <input value={form.connectionString} onChange={(e) => setForm({ ...form, connectionString: e.target.value })} className={inputCls + " font-mono text-xs"} placeholder={form.kind === "mysql" ? "mysql://user:pass@host:3306/db" : "postgresql://user:pass@host:5432/db"} />
                        <p className="text-xs text-stone-400 mt-1.5">If set, this URI is used directly (overrides the fields above). Handy when you already have a connection string.</p>
                      </Field>
                    </>
                  )}
                </>
              )}

              {msg.text && (
                <div className={`p-3 rounded-lg text-sm font-medium flex items-center gap-2 ${msg.type === "error" ? "bg-red-50 text-red-700 border border-red-100" : "bg-teal-50 text-teal-700 border border-teal-100"}`}>
                  {msg.type === "error" ? <AlertCircle size={16} /> : <Loader2 size={16} className="animate-spin" />} {msg.text}
                </div>
              )}
            </div>

            <div className="px-6 py-4 border-t border-stone-100 flex justify-end gap-3 sticky bottom-0 bg-white">
              <Button variant="secondary" disabled={busy} onClick={() => setModal(false)}>Cancel</Button>
              <Button variant="primary" icon={CheckCircle2} busy={busy} onClick={onSave}>{form.id ? "Update" : "Save"} Connector</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const inputCls = "w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">{label}</label>
      {children}
    </div>
  );
}
