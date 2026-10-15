"""Organization ontology template — the Enterprise Context Hierarchy (Team mode).

The default schema in Team mode: one central `Organization` hub, 19 Level-1 domains, and the concrete
node + edge types under each (with attributes). Goes well beyond a traditional data catalogue — it
models AI/ML, knowledge, processes, observability, and events as first-class, which is where the
Context Layer differentiates. Everything ultimately becomes an Event (see Events & Activities).

Stored in the shared `type_registry`, tagged source="organization", with `domain` + `fields` in
`properties_schema` so Curation can group and edit nodes/edges/attributes. Idempotent.
"""
from opencortyx.ontology.templates import build_rows, apply_template_rows

ORG_HUB = ("Organization", "The central organization this enterprise knowledge graph is about", [
    ("workspace_id", "string"), ("name", "string"), ("industry", "string"),
    ("size", "string"), ("headquarters", "string"), ("website", "string"),
])

ORG_DOMAINS = [
    "People & Teams", "Customers & External Parties", "Business", "Products & Services",
    "Projects & Initiatives", "Data & Analytics", "AI & ML", "Knowledge & Documentation",
    "Systems & Applications", "Infrastructure & Cloud", "Security & Governance", "Compliance & Risk",
    "Finance", "Assets", "Processes & Workflows", "Communications", "Events & Activities",
    "Goals & Strategy", "Observability & Operations",
]

ORG_NODES = {
    "People & Teams": [
        ("Employee", "An employee of the organization", [("name", "string"), ("email", "string"), ("title", "string")]),
        ("Team", "A team", [("name", "string")]),
        ("Department", "A department", [("name", "string")]),
        ("Role", "A role / position", [("title", "string"), ("level", "string")]),
        ("Skill", "A skill", [("name", "string"), ("level", "string")]),
        ("Contractor", "A contractor", [("name", "string"), ("agency", "string")]),
        ("Stakeholder", "An internal/external stakeholder", [("name", "string"), ("interest", "string")]),
    ],
    "Customers & External Parties": [
        ("Customer", "A customer", [("name", "string"), ("segment", "string"), ("tier", "string")]),
        ("Tenant", "A tenant (multi-tenant customer)", [("name", "string"), ("plan", "string")]),
        ("Vendor", "A vendor", [("name", "string"), ("category", "string")]),
        ("Partner", "A business partner", [("name", "string"), ("type", "string")]),
        ("Supplier", "A supplier", [("name", "string"), ("supplies", "string")]),
        ("Regulator", "A regulatory body", [("name", "string"), ("jurisdiction", "string")]),
    ],
    "Business": [
        ("BusinessUnit", "A business unit", [("name", "string")]),
        ("Capability", "A business capability", [("name", "string"), ("maturity", "string")]),
        ("BusinessProcess", "A business process", [("name", "string"), ("owner", "string")]),
        ("KPI", "A key performance indicator", [("name", "string"), ("target", "string"), ("unit", "string")]),
        ("Objective", "A business objective", [("name", "string"), ("status", "string")]),
        ("RevenueStream", "A revenue stream", [("name", "string"), ("amount", "float")]),
    ],
    "Products & Services": [
        ("Product", "A product", [("name", "string"), ("status", "string")]),
        ("Feature", "A product feature", [("name", "string"), ("status", "string")]),
        ("Service", "A service offering", [("name", "string")]),
        ("API", "An API", [("name", "string"), ("version", "string"), ("spec_url", "string")]),
        ("Release", "A product release", [("version", "string"), ("date", "date")]),
        ("Roadmap", "A product roadmap", [("name", "string"), ("horizon", "string")]),
    ],
    "Projects & Initiatives": [
        ("Project", "A project", [("name", "string"), ("status", "string"), ("owner", "string")]),
        ("Epic", "An epic", [("name", "string"), ("status", "string")]),
        ("Sprint", "A sprint", [("name", "string"), ("start", "date"), ("end", "date")]),
        ("Task", "A task", [("name", "string"), ("status", "string"), ("assignee", "string")]),
        ("Milestone", "A milestone", [("name", "string"), ("due", "date")]),
        ("Decision", "A recorded decision", [("title", "string"), ("date", "date"), ("rationale", "string")]),
    ],
    "Data & Analytics": [
        ("DataProduct", "A data product", [("name", "string"), ("owner", "string"), ("domain", "string")]),
        ("Dataset", "A dataset", [("name", "string"), ("rows", "int")]),
        ("Database", "A database", [("name", "string"), ("engine", "string")]),
        ("Schema", "A database schema", [("name", "string")]),
        ("Table", "A table", [("name", "string"), ("rows", "int")]),
        ("Column", "A column", [("name", "string"), ("data_type", "string"), ("is_pii", "bool")]),
        ("File", "A data file", [("path", "string"), ("format", "string")]),
        ("Dashboard", "A dashboard", [("name", "string"), ("tool", "string")]),
        ("Report", "A report", [("name", "string")]),
        ("Metric", "A metric / measure", [("name", "string"), ("unit", "string")]),
        ("Query", "A saved query", [("name", "string"), ("sql", "string")]),
        ("DataContract", "A data contract", [("name", "string"), ("sla", "string")]),
    ],
    "AI & ML": [
        ("AIAgent", "An AI agent", [("name", "string"), ("purpose", "string")]),
        ("MLModel", "A machine-learning model", [("name", "string"), ("version", "string"), ("task", "string")]),
        ("Prompt", "A prompt template", [("name", "string"), ("text", "string")]),
        ("Embedding", "An embedding model / set", [("name", "string"), ("dimensions", "int")]),
        ("VectorIndex", "A vector index", [("name", "string"), ("metric", "string")]),
        ("FeatureStore", "A feature store", [("name", "string")]),
        ("Evaluation", "A model / answer evaluation", [("name", "string"), ("score", "float")]),
        ("Experiment", "An ML experiment", [("name", "string"), ("status", "string")]),
        ("TrainingDataset", "A training dataset", [("name", "string"), ("size", "int")]),
        ("Inference", "An inference / prediction run", [("at", "datetime"), ("latency_ms", "int")]),
    ],
    "Knowledge & Documentation": [
        ("Wiki", "A wiki page", [("title", "string"), ("url", "string")]),
        ("DesignDoc", "A design document", [("title", "string"), ("author", "string")]),
        ("Runbook", "An operational runbook", [("title", "string")]),
        ("ADR", "An architecture decision record", [("title", "string"), ("status", "string")]),
        ("SOP", "A standard operating procedure", [("title", "string")]),
        ("MeetingNotes", "Meeting notes", [("title", "string"), ("date", "date")]),
        ("IncidentReport", "An incident report / post-mortem", [("title", "string"), ("severity", "string")]),
        ("Research", "A research note / paper", [("title", "string"), ("author", "string")]),
    ],
    "Systems & Applications": [
        ("Application", "An application", [("name", "string"), ("owner", "string")]),
        ("SaaSTool", "A SaaS tool (e.g. GitHub, Slack, Jira)", [("name", "string"), ("vendor", "string")]),
        ("InternalService", "An internal service", [("name", "string"), ("language", "string")]),
        ("Repository", "A code repository", [("name", "string"), ("url", "string")]),
        ("APIGateway", "An API gateway", [("name", "string")]),
        ("Queue", "A message queue / topic", [("name", "string"), ("kind", "string")]),
        ("Integration", "An integration between systems", [("name", "string")]),
    ],
    "Infrastructure & Cloud": [
        ("CloudAccount", "A cloud account / subscription", [("provider", "string"), ("account_id", "string")]),
        ("Region", "A cloud region", [("name", "string"), ("provider", "string")]),
        ("Cluster", "A compute cluster", [("name", "string"), ("kind", "string")]),
        ("VM", "A virtual machine", [("name", "string"), ("size", "string")]),
        ("Container", "A container", [("image", "string")]),
        ("KubernetesNamespace", "A Kubernetes namespace", [("name", "string")]),
        ("StorageBucket", "An object-storage bucket", [("name", "string"), ("provider", "string")]),
        ("DataLake", "A data lake", [("name", "string")]),
        ("Network", "A network / VPC", [("name", "string"), ("cidr", "string")]),
    ],
    "Security & Governance": [
        ("Policy", "A governance / security policy", [("name", "string"), ("scope", "string")]),
        ("Classification", "A data classification", [("name", "string"), ("level", "string")]),
        ("Tag", "A governance tag", [("name", "string")]),
        ("Owner", "A data/asset owner", [("name", "string")]),
        ("Steward", "A data steward", [("name", "string")]),
        ("AccessRole", "An access role", [("name", "string")]),
        ("Permission", "A permission / grant", [("name", "string"), ("action", "string")]),
        ("Secret", "A secret / credential reference", [("name", "string"), ("store", "string")]),
        ("SecurityControl", "A security control", [("name", "string"), ("framework", "string")]),
    ],
    "Compliance & Risk": [
        ("Regulation", "A regulation / standard", [("name", "string"), ("jurisdiction", "string")]),
        ("Control", "A compliance control", [("name", "string"), ("status", "string")]),
        ("Audit", "An audit", [("name", "string"), ("date", "date")]),
        ("Risk", "A risk", [("name", "string"), ("likelihood", "string"), ("impact", "string")]),
        ("Finding", "An audit / risk finding", [("title", "string"), ("severity", "string")]),
        ("Exception", "A policy exception", [("title", "string"), ("expires_on", "date")]),
    ],
    "Finance": [
        ("Budget", "A budget", [("name", "string"), ("amount", "float"), ("period", "string")]),
        ("Expense", "An expense", [("amount", "float"), ("category", "string"), ("date", "date")]),
        ("Invoice", "An invoice", [("number", "string"), ("amount", "float"), ("status", "string")]),
        ("Revenue", "A revenue record", [("amount", "float"), ("period", "string")]),
        ("Contract", "A contract", [("title", "string"), ("counterparty", "string"), ("value", "float")]),
        ("Subscription", "A subscription", [("name", "string"), ("cost", "float"), ("renews_on", "date")]),
    ],
    "Assets": [
        ("Server", "A physical/virtual server", [("name", "string"), ("location", "string")]),
        ("Device", "A device", [("name", "string"), ("type", "string")]),
        ("Vehicle", "A vehicle", [("make", "string"), ("registration", "string")]),
        ("Building", "A building / facility", [("name", "string"), ("address", "string")]),
        ("License", "A software / business license", [("name", "string"), ("seats", "int"), ("expires_on", "date")]),
        ("IntellectualProperty", "An IP asset (patent, trademark)", [("name", "string"), ("type", "string")]),
    ],
    "Processes & Workflows": [
        ("Workflow", "A workflow", [("name", "string"), ("owner", "string")]),
        ("DAG", "A directed acyclic graph of tasks", [("name", "string")]),
        ("Pipeline", "A data/ML pipeline", [("name", "string"), ("status", "string")]),
        ("Job", "A job", [("name", "string"), ("schedule", "string")]),
        ("Trigger", "A trigger", [("name", "string"), ("kind", "string")]),
        ("Schedule", "A schedule", [("cron", "string"), ("timezone", "string")]),
    ],
    "Communications": [
        ("Email", "An email", [("subject", "string"), ("from", "string"), ("sent_at", "datetime")]),
        ("SlackMessage", "A chat message", [("channel", "string"), ("sent_at", "datetime")]),
        ("Ticket", "A support / work ticket", [("key", "string"), ("status", "string"), ("priority", "string")]),
        ("Meeting", "A meeting", [("title", "string"), ("at", "datetime")]),
        ("Call", "A call", [("with", "string"), ("at", "datetime")]),
        ("Announcement", "An announcement", [("title", "string"), ("at", "datetime")]),
    ],
    "Events & Activities": [
        ("DeploymentEvent", "A deployment event", [("service", "string"), ("version", "string"), ("at", "datetime")]),
        ("DataChangeEvent", "A data change event", [("asset", "string"), ("change", "string"), ("at", "datetime")]),
        ("IncidentEvent", "An incident event", [("severity", "string"), ("at", "datetime")]),
        ("AIInteraction", "An AI interaction / agent run", [("agent", "string"), ("at", "datetime")]),
        ("LoginEvent", "A login event", [("actor", "string"), ("at", "datetime"), ("ip", "string")]),
        ("CustomerEvent", "A customer event", [("customer", "string"), ("type", "string"), ("at", "datetime")]),
        ("TransactionEvent", "A transaction event", [("amount", "float"), ("at", "datetime")]),
        ("PipelineRun", "A pipeline run", [("pipeline", "string"), ("status", "string"), ("at", "datetime")]),
        ("Alert", "An alert", [("name", "string"), ("severity", "string"), ("at", "datetime")]),
    ],
    "Goals & Strategy": [
        ("OKR", "An objective and key results", [("objective", "string"), ("period", "string")]),
        ("Initiative", "A strategic initiative", [("name", "string"), ("status", "string")]),
        ("Outcome", "A measurable outcome", [("name", "string"), ("value", "string")]),
    ],
    "Observability & Operations": [
        ("Log", "A log stream / record", [("source", "string"), ("level", "string")]),
        ("Trace", "A distributed trace", [("trace_id", "string"), ("service", "string")]),
        ("SLO", "A service-level objective", [("name", "string"), ("target", "string")]),
        ("SLA", "A service-level agreement", [("name", "string"), ("target", "string")]),
        ("Incident", "An operational incident", [("title", "string"), ("severity", "string"), ("status", "string")]),
        ("RootCause", "A root cause", [("description", "string")]),
    ],
}

ORG_EDGES = {
    "People & Teams": [
        ("WORKS_FOR", "employee works for the organization", ["Employee", "Contractor"], ["Organization", "Department"]),
        ("MEMBER_OF", "member of a team", ["Employee", "Contractor"], ["Team"]),
        ("REPORTS_TO", "reporting line", ["Employee"], ["Employee", "Role"]),
        ("MANAGES", "manages a team / person", ["Employee"], ["Team", "Employee", "Department"]),
        ("COLLABORATES_WITH", "collaborates with", ["Employee", "Team"], ["Employee", "Team"]),
    ],
    "Customers & External Parties": [
        ("SERVES", "the org serves a customer", ["Organization"], ["Customer", "Tenant"]),
        ("CONTRACTED_WITH", "has a contract with a party", ["Organization"], ["Vendor", "Partner", "Supplier", "Customer"]),
        ("PARTNERS_WITH", "partners with", ["Organization"], ["Partner"]),
        ("SUPPLIES", "supplier supplies the org", ["Supplier", "Vendor"], ["Organization", "Product"]),
    ],
    "Business": [
        ("OWNS", "owns a capability / process / asset", ["BusinessUnit", "Team", "Employee"], ["Capability", "BusinessProcess", "Product", "DataProduct"]),
        ("TRACKS", "tracks a KPI / metric", ["BusinessUnit", "Team", "Objective"], ["KPI", "Metric", "Outcome"]),
        ("SUPPORTS", "supports a capability / objective", ["BusinessProcess", "Service", "Application"], ["Capability", "Objective"]),
        ("DRIVES", "drives an objective / revenue", ["Initiative", "Product", "Capability"], ["Objective", "RevenueStream"]),
    ],
    "Products & Services": [
        ("OWNS_PRODUCT", "team owns a product", ["Team", "BusinessUnit"], ["Product"]),
        ("DEPENDS_ON", "depends on another thing", ["Product", "Feature", "Service", "Application", "InternalService", "Project", "Pipeline"], ["Product", "Feature", "Service", "API", "Application", "InternalService", "Dataset"]),
        ("EXPOSES_API", "exposes an API", ["Product", "Service", "Application", "InternalService"], ["API"]),
        ("RELEASED_AS", "shipped as a release", ["Product", "Feature"], ["Release"]),
    ],
    "Projects & Initiatives": [
        ("CONTAINS", "structural containment", ["Project", "Epic", "Sprint", "Database", "Schema", "Table", "Dataset"], ["Epic", "Sprint", "Task", "Schema", "Table", "Column"]),
        ("BLOCKED_BY", "blocked by", ["Task", "Epic", "Project"], ["Task", "Risk", "Decision"]),
        ("DELIVERS", "delivers an outcome / milestone", ["Project", "Initiative", "Sprint"], ["Milestone", "Outcome", "Feature"]),
    ],
    "Data & Analytics": [
        ("QUERIES", "reads/queries data", ["Query", "Dashboard", "Report", "Application", "MLModel"], ["Table", "Dataset", "Database"]),
        ("POWERS", "powers a downstream asset", ["Dataset", "Table", "Metric", "DataProduct"], ["Dashboard", "Report", "MLModel", "Metric"]),
        ("GENERATES", "produces / generates", ["Pipeline", "Job", "MLModel", "AIAgent", "Application", "Service"], ["Dataset", "File", "Report", "Inference", "Embedding", "Alert"]),
        ("USES", "uses an asset", ["Dashboard", "Report", "Application", "MLModel"], ["Dataset", "Metric", "FeatureStore", "API"]),
    ],
    "AI & ML": [
        ("TRAINS", "trains a model on data", ["Experiment", "Pipeline", "Employee"], ["MLModel"]),
        ("USES_CONTEXT", "uses retrieved context", ["AIAgent", "Prompt", "Inference"], ["VectorIndex", "Dataset", "Wiki", "DataProduct"]),
        ("USES_MODEL", "uses a model", ["AIAgent", "Inference", "Application", "Feature"], ["MLModel", "Embedding"]),
        ("EVALUATED_BY", "evaluated by", ["MLModel", "AIAgent", "Prompt"], ["Evaluation"]),
    ],
    "Knowledge & Documentation": [
        ("REFERENCES", "references another artifact", ["Wiki", "DesignDoc", "ADR", "Runbook", "Research", "IncidentReport"], ["Wiki", "DesignDoc", "ADR", "Table", "Application", "MLModel", "Decision"]),
        ("CREATED_BY", "authored by", ["Wiki", "DesignDoc", "Runbook", "ADR", "SOP", "Research", "Report", "Decision"], ["Employee", "Team"]),
        ("DERIVED_FROM", "derived from", ["DesignDoc", "Report", "Dataset", "MLModel"], ["Research", "Dataset", "MeetingNotes"]),
    ],
    "Systems & Applications": [
        ("INTEGRATES_WITH", "integrates with another system", ["Application", "SaaSTool", "InternalService", "Integration"], ["Application", "SaaSTool", "InternalService", "API"]),
        ("CALLS", "calls a service / API", ["Application", "InternalService", "AIAgent"], ["InternalService", "API", "APIGateway"]),
        ("HOSTS", "hosts an application / service", ["CloudAccount", "Cluster", "VM", "Server"], ["Application", "InternalService", "Container"]),
    ],
    "Infrastructure & Cloud": [
        ("DEPLOYED_ON", "deployed on infrastructure", ["Application", "InternalService", "Container", "MLModel"], ["Cluster", "VM", "KubernetesNamespace", "Server", "CloudAccount"]),
        ("RUNS_IN", "runs in a region / namespace", ["Cluster", "VM", "Container", "Application"], ["Region", "KubernetesNamespace", "CloudAccount"]),
        ("STORES_IN", "stores data in", ["Dataset", "File", "DataLake", "Application"], ["StorageBucket", "Database", "DataLake"]),
        ("CONNECTED_TO", "network connection", ["VM", "Container", "Cluster", "Server"], ["Network", "VM", "Server"]),
    ],
    "Security & Governance": [
        ("CLASSIFIED_AS", "classified as", ["Dataset", "Table", "Column", "File", "Document"], ["Classification"]),
        ("TAGGED_WITH", "tagged with a governance tag", ["Dataset", "Table", "Column", "Product", "Application"], ["Tag"]),
        ("HAS_ACCESS", "principal has access", ["Employee", "AccessRole", "AIAgent", "Application"], ["Dataset", "Table", "Application", "Secret", "API"]),
        ("PROTECTED_BY", "protected by a control", ["Dataset", "Application", "InternalService", "CloudAccount"], ["SecurityControl", "Policy"]),
        ("STEWARDED_BY", "stewarded by", ["Dataset", "DataProduct", "Table"], ["Steward", "Owner"]),
    ],
    "Compliance & Risk": [
        ("COMPLIES_WITH", "complies with a regulation", ["Organization", "BusinessProcess", "Control"], ["Regulation"]),
        ("VIOLATES", "violates a policy / regulation", ["Finding", "Risk", "Exception"], ["Policy", "Regulation", "Control"]),
        ("AUDITED_BY", "audited by", ["Organization", "BusinessProcess", "Control", "Application"], ["Audit", "Regulator"]),
        ("MITIGATED_BY", "risk mitigated by a control", ["Risk", "Finding"], ["Control", "SecurityControl"]),
    ],
    "Finance": [
        ("HAS_BUDGET", "has a budget", ["Organization", "Department", "Project"], ["Budget"]),
        ("INCURS", "incurs an expense", ["Department", "Project", "Team"], ["Expense", "Invoice"]),
        ("BILLED_VIA", "billed via a contract / subscription", ["Vendor", "SaaSTool", "Service"], ["Invoice", "Subscription", "Contract"]),
    ],
    "Assets": [
        ("OWNS_ASSET", "owns a physical/IP asset", ["Organization", "Department"], ["Server", "Device", "Vehicle", "Building", "License", "IntellectualProperty"]),
    ],
    "Processes & Workflows": [
        ("TRIGGERS", "triggers a job / workflow", ["Trigger", "Schedule", "Event"], ["Job", "Pipeline", "Workflow", "DAG"]),
        ("EXECUTES", "executes a job / task", ["Workflow", "DAG", "Pipeline"], ["Job", "Task"]),
        ("PRODUCES", "produces an output", ["Pipeline", "Job", "Workflow"], ["Dataset", "File", "Report", "PipelineRun"]),
    ],
    "Communications": [
        ("DISCUSSED_IN", "discussed in a thread / meeting", ["Decision", "Incident", "Project", "Risk"], ["SlackMessage", "Meeting", "Email", "Ticket"]),
        ("MENTIONED_IN", "mentioned in a communication", ["Employee", "Customer", "Product", "Incident"], ["Email", "SlackMessage", "MeetingNotes", "Ticket"]),
        ("CREATED_FROM", "created from a communication", ["Ticket", "Task", "IncidentReport", "Decision"], ["Email", "SlackMessage", "Meeting"]),
    ],
    "Events & Activities": [
        ("AFFECTED", "an event affected an entity", ["DeploymentEvent", "IncidentEvent", "DataChangeEvent", "Alert"], ["Application", "Dataset", "Customer", "InternalService", "SLO"]),
        ("TRIGGERED", "an event/condition triggered another", ["Alert", "DataChangeEvent", "DeploymentEvent", "Trigger"], ["IncidentEvent", "PipelineRun", "Job", "Alert"]),
        ("OBSERVED", "observed / recorded an event", ["Application", "InternalService", "Pipeline", "AIAgent"], ["LoginEvent", "TransactionEvent", "CustomerEvent", "AIInteraction"]),
    ],
    "Goals & Strategy": [
        ("HAS_OBJECTIVE", "has an objective / OKR", ["Organization", "Department", "Team"], ["Objective", "OKR"]),
        ("MEASURED_BY", "measured by a KPI / outcome", ["Objective", "OKR", "Initiative"], ["KPI", "Outcome", "Metric"]),
    ],
    "Observability & Operations": [
        ("GENERATED_BY", "telemetry generated by", ["Log", "Metric", "Trace", "Alert"], ["Application", "InternalService", "Pipeline", "VM", "Container"]),
        ("CAUSED", "root cause caused an incident", ["RootCause", "DeploymentEvent", "DataChangeEvent"], ["Incident", "IncidentEvent", "Alert"]),
        ("MEETS", "service meets an SLO/SLA", ["InternalService", "Application", "DataContract"], ["SLO", "SLA"]),
    ],
}


async def seed_org_ontology():
    """Idempotently register the full Organization (Enterprise Context) ontology (source='organization')."""
    rows = build_rows(ORG_HUB, ORG_NODES, ORG_EDGES, source="organization")
    n = await apply_template_rows(rows, hub_name=ORG_HUB[0])
    return {"seeded": n, "domains": len(ORG_DOMAINS)}
