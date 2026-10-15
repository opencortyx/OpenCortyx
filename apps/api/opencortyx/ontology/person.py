"""Person ontology template — the default Person-centric graph schema (Person mode).

A complete, opinionated ontology for modelling a single human: one central `Person` hub, 19 Level-1
domains, and the concrete node + edge types under each (with sensible attributes). It is the default
schema in Person mode (vs Team mode = the data-catalogue ontology in seed.py).

Stored in the SAME `type_registry` table as the base ontology, tagged `source="person"` and carrying a
`domain` + `fields` (attributes) in `properties_schema` so the Curation/Ontology UI can group and show
nodes, edges, and attributes. Idempotent: upserts by (name, kind), unioning edge endpoints across the
domains that reuse an edge/node name (e.g. OWNS, FOLLOWS, Company, Website).

Everything ultimately becomes an Event — see the Events domain.
"""
from opencortyx.ontology.templates import build_rows, apply_template_rows

# The central hub + its attributes.
PERSON_HUB = ("Person", "The central individual this knowledge graph is about", [
    ("person_id", "string"), ("name", "string"), ("date_of_birth", "date"),
    ("gender", "string"), ("nationality", "string"), ("email", "string"), ("phone", "string"),
])

# Ordered 19 Level-1 domains (grouping shown in the UI).
PERSON_DOMAINS = [
    "Identity", "Employment", "Education", "Health", "Finance", "Relationships", "Location",
    "Devices", "Communication", "Skills", "Interests", "Activities", "Documents", "Organizations",
    "Assets", "Digital Presence", "Goals", "Events", "Preferences",
]

# domain -> [(node_name, description, [(attr, type), ...]), ...]
PERSON_NODES = {
    "Identity": [
        ("Identity", "Core identity record", [("legal_name", "string"), ("aliases", "string[]"), ("verified", "bool")]),
        ("Passport", "Passport document", [("passport_number", "string"), ("country", "string"), ("issue_date", "date"), ("expiry_date", "date")]),
        ("DriverLicense", "Driving license", [("license_number", "string"), ("class", "string"), ("expiry_date", "date")]),
        ("Aadhaar", "Aadhaar national ID (India)", [("aadhaar_number", "string"), ("issued_on", "date")]),
        ("PAN", "PAN tax ID (India)", [("pan_number", "string")]),
        ("Biometrics", "Biometric identifiers", [("type", "string"), ("template_ref", "string")]),
    ],
    "Employment": [
        ("Company", "An employer / company", [("name", "string"), ("industry", "string"), ("website", "string")]),
        ("Department", "A department within a company", [("name", "string")]),
        ("Team", "A team within a department", [("name", "string")]),
        ("Role", "A job role / position", [("title", "string"), ("seniority", "string"), ("start_date", "date"), ("end_date", "date")]),
        ("Project", "A work project", [("name", "string"), ("status", "string")]),
        ("Manager", "A reporting manager", [("name", "string")]),
        ("Job", "A held job / employment", [("title", "string"), ("employment_type", "string")]),
    ],
    "Education": [
        ("School", "A school attended", [("name", "string"), ("location", "string")]),
        ("College", "A college attended", [("name", "string"), ("location", "string")]),
        ("University", "A university attended", [("name", "string"), ("location", "string")]),
        ("Institution", "An educational institution", [("name", "string"), ("type", "string")]),
        ("Course", "A course enrolled in", [("name", "string"), ("provider", "string"), ("year", "int")]),
        ("Degree", "A degree earned", [("name", "string"), ("field", "string"), ("year", "int")]),
        ("Certification", "A certificate / certification", [("name", "string"), ("issuer", "string"), ("issued_on", "date"), ("expires_on", "date")]),
        ("Certificate", "A certificate document", [("name", "string"), ("issuer", "string")]),
    ],
    "Health": [
        ("Disease", "A medical condition / disease", [("name", "string"), ("icd_code", "string"), ("status", "string")]),
        ("Medication", "A medication taken", [("name", "string"), ("dosage", "string"), ("frequency", "string")]),
        ("Doctor", "A healthcare provider", [("name", "string"), ("specialty", "string")]),
        ("Hospital", "A hospital / clinic", [("name", "string"), ("location", "string")]),
        ("LabResult", "A lab test result", [("test", "string"), ("value", "string"), ("unit", "string"), ("taken_on", "date")]),
        ("Allergy", "An allergy", [("allergen", "string"), ("severity", "string")]),
        ("FitnessMetric", "A fitness / vital metric", [("metric", "string"), ("value", "float"), ("unit", "string"), ("measured_on", "date")]),
    ],
    "Finance": [
        ("BankAccount", "A bank account", [("account_number", "string"), ("bank", "string"), ("type", "string"), ("currency", "string")]),
        ("Transaction", "A financial transaction", [("amount", "float"), ("currency", "string"), ("merchant", "string"), ("date", "date")]),
        ("CreditCard", "A credit / debit card", [("card_number_masked", "string"), ("issuer", "string"), ("expiry", "string")]),
        ("Loan", "A loan", [("principal", "float"), ("rate", "float"), ("lender", "string"), ("status", "string")]),
        ("Investment", "An investment holding", [("instrument", "string"), ("quantity", "float"), ("value", "float")]),
        ("TaxRecord", "A tax filing record", [("year", "int"), ("amount", "float"), ("jurisdiction", "string")]),
    ],
    "Relationships": [
        ("FamilyUnit", "A family unit / household", [("name", "string")]),
        ("Contact", "A personal contact", [("name", "string"), ("relationship", "string"), ("phone", "string")]),
    ],
    "Location": [
        ("Address", "A postal address", [("line1", "string"), ("city", "string"), ("postal_code", "string"), ("country", "string")]),
        ("City", "A city", [("name", "string"), ("country", "string")]),
        ("Country", "A country", [("name", "string"), ("iso_code", "string")]),
        ("Building", "A building / place", [("name", "string"), ("address", "string")]),
        ("GPSLocation", "A GPS coordinate", [("lat", "float"), ("lng", "float"), ("recorded_at", "datetime")]),
    ],
    "Devices": [
        ("Phone", "A phone device", [("make", "string"), ("model", "string"), ("os", "string")]),
        ("Laptop", "A laptop / computer", [("make", "string"), ("model", "string"), ("os", "string")]),
        ("Tablet", "A tablet device", [("make", "string"), ("model", "string")]),
        ("IoTDevice", "An IoT / smart device", [("type", "string"), ("make", "string")]),
        ("Browser", "A web browser used", [("name", "string"), ("version", "string")]),
    ],
    "Communication": [
        ("Email", "An email message", [("subject", "string"), ("from", "string"), ("to", "string"), ("sent_at", "datetime")]),
        ("Message", "A chat / SMS message", [("channel", "string"), ("sent_at", "datetime")]),
        ("Call", "A phone / video call", [("with", "string"), ("duration_sec", "int"), ("at", "datetime")]),
        ("Meeting", "A meeting", [("title", "string"), ("at", "datetime"), ("location", "string")]),
    ],
    "Skills": [
        ("Skill", "A skill", [("name", "string"), ("level", "string")]),
        ("Technology", "A technology / tool known", [("name", "string"), ("category", "string")]),
        ("Expertise", "An area of expertise", [("area", "string"), ("years", "int")]),
    ],
    "Interests": [
        ("Topic", "A topic of interest", [("name", "string")]),
        ("Hobby", "A hobby", [("name", "string")]),
        ("Community", "A community / group", [("name", "string"), ("platform", "string")]),
    ],
    "Activities": [
        ("Product", "A product interacted with", [("name", "string"), ("category", "string"), ("price", "float")]),
        ("Website", "A website / web page", [("url", "string"), ("title", "string")]),
        ("SearchQuery", "A search query", [("text", "string"), ("at", "datetime")]),
    ],
    "Documents": [
        ("PDF", "A PDF document", [("title", "string"), ("pages", "int")]),
        ("Contract", "A contract document", [("title", "string"), ("counterparty", "string"), ("signed_on", "date")]),
        ("Resume", "A resume / CV", [("title", "string"), ("version", "string")]),
        ("Report", "A report document", [("title", "string"), ("date", "date")]),
    ],
    "Organizations": [
        ("NGO", "A non-profit / NGO", [("name", "string"), ("cause", "string")]),
        ("Club", "A club / society", [("name", "string")]),
        ("GovernmentBody", "A government body", [("name", "string"), ("jurisdiction", "string")]),
    ],
    "Assets": [
        ("House", "A house / property", [("address", "string"), ("value", "float"), ("ownership", "string")]),
        ("Vehicle", "A vehicle", [("make", "string"), ("model", "string"), ("registration", "string")]),
        ("Land", "A plot of land", [("location", "string"), ("area", "string"), ("value", "float")]),
        ("Equipment", "A piece of equipment", [("name", "string"), ("value", "float")]),
    ],
    "Digital Presence": [
        ("SocialAccount", "A social-media account", [("platform", "string"), ("handle", "string"), ("followers", "int")]),
        ("Post", "A social post", [("platform", "string"), ("text", "string"), ("posted_at", "datetime")]),
    ],
    "Goals": [
        ("Goal", "A personal / professional goal", [("name", "string"), ("target_date", "date"), ("status", "string")]),
        ("Milestone", "A milestone toward a goal", [("name", "string"), ("due", "date")]),
        ("Task", "A task / action item", [("name", "string"), ("status", "string"), ("due", "date")]),
    ],
    "Events": [
        ("Event", "A generic event (everything becomes an event)", [("name", "string"), ("type", "string"), ("at", "datetime")]),
        ("Interview", "An interview event", [("with", "string"), ("at", "datetime"), ("outcome", "string")]),
        ("Purchase", "A purchase event", [("item", "string"), ("amount", "float"), ("at", "datetime")]),
        ("HospitalVisit", "A hospital visit event", [("reason", "string"), ("at", "datetime"), ("provider", "string")]),
        ("Flight", "A flight event", [("from", "string"), ("to", "string"), ("at", "datetime")]),
    ],
    "Preferences": [
        ("Preference", "A stated preference / setting", [("key", "string"), ("value", "string"), ("category", "string")]),
    ],
}

# domain -> [(edge_name, description, [source_types], [target_types]), ...]
PERSON_EDGES = {
    "Identity": [
        ("HAS_IDENTITY", "links a person to their identity record", ["Person"], ["Identity"]),
        ("HAS_PASSPORT", "person holds a passport", ["Person"], ["Passport"]),
        ("HAS_LICENSE", "person holds a driving license", ["Person"], ["DriverLicense"]),
        ("HAS_GOVT_ID", "person holds a government ID", ["Person"], ["Aadhaar", "PAN"]),
        ("HAS_BIOMETRIC", "person has biometric identifiers", ["Person"], ["Biometrics"]),
    ],
    "Employment": [
        ("WORKS_FOR", "current employer", ["Person"], ["Company"]),
        ("HAS_ROLE", "holds a role / job", ["Person"], ["Role", "Job"]),
        ("REPORTS_TO", "reporting line", ["Person"], ["Person", "Manager"]),
        ("MANAGES", "manages another person", ["Person"], ["Person"]),
        ("WORKED_AT", "past employer", ["Person"], ["Company"]),
        ("IN_DEPARTMENT", "belongs to a department", ["Person", "Team"], ["Department"]),
        ("ON_PROJECT", "works on a project", ["Person"], ["Project"]),
    ],
    "Education": [
        ("STUDIED_AT", "attended an institution", ["Person"], ["School", "College", "University", "Institution"]),
        ("ENROLLED_IN", "enrolled in a course", ["Person"], ["Course"]),
        ("EARNED", "earned a degree", ["Person"], ["Degree"]),
        ("HAS_CERTIFICATION", "holds a certification", ["Person"], ["Certification", "Certificate"]),
    ],
    "Health": [
        ("HAS_CONDITION", "has a medical condition", ["Person"], ["Disease"]),
        ("TAKES_MEDICATION", "takes a medication", ["Person"], ["Medication"]),
        ("VISITED_PROVIDER", "visited a provider", ["Person"], ["Doctor", "Hospital"]),
        ("HAS_LAB_RESULT", "has a lab result", ["Person"], ["LabResult"]),
        ("HAS_ALLERGY", "has an allergy", ["Person"], ["Allergy"]),
        ("HAS_VITAL_SIGN", "has a vital / fitness metric", ["Person"], ["FitnessMetric"]),
    ],
    "Finance": [
        ("OWNS_ACCOUNT", "owns a bank account", ["Person"], ["BankAccount"]),
        ("PERFORMED_TRANSACTION", "performed a transaction", ["Person"], ["Transaction"]),
        ("OWNS_CARD", "owns a card", ["Person"], ["CreditCard"]),
        ("HAS_LOAN", "has a loan", ["Person"], ["Loan"]),
        ("HAS_INVESTMENT", "has an investment", ["Person"], ["Investment"]),
        ("PAYS_BILL", "pays a bill", ["Person"], ["Transaction"]),
    ],
    "Relationships": [
        ("SPOUSE_OF", "spouse relationship", ["Person"], ["Person"]),
        ("CHILD_OF", "child relationship", ["Person"], ["Person"]),
        ("PARENT_OF", "parent relationship", ["Person"], ["Person"]),
        ("FRIEND_OF", "friend relationship", ["Person"], ["Person"]),
        ("COLLEAGUE_OF", "colleague relationship", ["Person"], ["Person"]),
        ("RELATED_TO", "generic relationship", ["Person"], ["Person", "FamilyUnit", "Contact"]),
    ],
    "Location": [
        ("LIVES_AT", "place of residence", ["Person"], ["Address"]),
        ("VISITED", "visited a place", ["Person"], ["Building", "City", "Country", "GPSLocation"]),
        ("WORKS_AT", "place of work", ["Person"], ["Address", "Building"]),
        ("TRAVELLED_TO", "travelled to", ["Person"], ["City", "Country"]),
        ("CHECKED_IN_AT", "checked in at", ["Person"], ["Building", "GPSLocation"]),
    ],
    "Devices": [
        ("OWNS_DEVICE", "owns a device", ["Person"], ["Phone", "Laptop", "Tablet", "IoTDevice"]),
        ("LOGGED_IN_FROM", "logged in from a device", ["Person"], ["Phone", "Laptop", "Tablet"]),
        ("USES_BROWSER", "uses a browser", ["Person"], ["Browser"]),
        ("CONNECTED_TO", "device connected to", ["Phone", "Laptop", "IoTDevice"], ["GPSLocation", "Building"]),
    ],
    "Communication": [
        ("SENT_EMAIL", "sent an email", ["Person"], ["Email"]),
        ("RECEIVED_EMAIL", "received an email", ["Person"], ["Email"]),
        ("SENT_MESSAGE", "sent a message", ["Person"], ["Message"]),
        ("CALLED", "made a call", ["Person"], ["Call"]),
        ("ATTENDED_MEETING", "attended a meeting", ["Person"], ["Meeting"]),
    ],
    "Skills": [
        ("HAS_SKILL", "has a skill", ["Person"], ["Skill"]),
        ("HAS_EXPERTISE", "has expertise", ["Person"], ["Expertise", "Technology"]),
    ],
    "Interests": [
        ("INTERESTED_IN", "interested in a topic", ["Person"], ["Topic", "Hobby"]),
        ("FOLLOWS", "follows a topic / community / account", ["Person"], ["Topic", "Community", "SocialAccount"]),
        ("LIKES", "likes something", ["Person"], ["Topic", "Hobby", "Post", "Product"]),
        ("SUBSCRIBED_TO", "subscribed to", ["Person"], ["Community", "Website"]),
    ],
    "Activities": [
        ("PURCHASED", "purchased a product", ["Person"], ["Product"]),
        ("VIEWED", "viewed something", ["Person"], ["Product", "Website", "Post"]),
        ("CLICKED", "clicked something", ["Person"], ["Website", "Post", "Product"]),
        ("SEARCHED", "ran a search", ["Person"], ["SearchQuery"]),
        ("ATTENDED", "attended an event", ["Person"], ["Event", "Meeting"]),
    ],
    "Documents": [
        ("CREATED", "created a document / item", ["Person"], ["PDF", "Contract", "Resume", "Report", "Post", "Event"]),
        ("OWNS", "owns a document / asset", ["Person"], ["PDF", "Contract", "Resume", "Report", "House", "Vehicle", "Land", "Equipment"]),
        ("SIGNED", "signed a document", ["Person"], ["Contract", "PDF"]),
        ("SHARED", "shared a document", ["Person"], ["PDF", "Report", "Post"]),
    ],
    "Organizations": [
        ("MEMBER_OF", "member of an organization", ["Person"], ["Company", "NGO", "Club", "GovernmentBody", "Community"]),
        ("FOUNDED", "founded an organization", ["Person"], ["Company", "NGO", "Club"]),
        ("INVESTED_IN", "invested in an organization", ["Person"], ["Company", "Investment"]),
        ("VOLUNTEERS_FOR", "volunteers for", ["Person"], ["NGO", "Club"]),
    ],
    "Assets": [
        ("RENTS", "rents an asset", ["Person"], ["House", "Vehicle", "Equipment"]),
        ("LEASES", "leases an asset", ["Person"], ["House", "Vehicle", "Land"]),
        ("SOLD", "sold an asset", ["Person"], ["House", "Vehicle", "Land", "Equipment"]),
    ],
    "Digital Presence": [
        ("HAS_ACCOUNT", "has a digital account", ["Person"], ["SocialAccount"]),
        ("POSTED", "made a post", ["Person"], ["Post"]),
        ("COMMENTED", "commented", ["Person"], ["Post"]),
        ("MENTIONED", "mentioned someone / something", ["Person", "Post"], ["Person", "Topic", "SocialAccount"]),
    ],
    "Goals": [
        ("HAS_GOAL", "has a goal", ["Person"], ["Goal"]),
        ("COMPLETED_GOAL", "completed a goal", ["Person"], ["Goal"]),
        ("TRACKS", "tracks a goal / metric", ["Person"], ["Goal", "Milestone", "FitnessMetric"]),
        ("PLANNED", "planned a task / event", ["Person"], ["Task", "Event"]),
    ],
    "Events": [
        ("ATTENDED_EVENT", "attended an event", ["Person"], ["Event", "Meeting", "Interview"]),
        ("CREATED_EVENT", "created an event", ["Person"], ["Event", "Meeting"]),
        ("PARTICIPATED_IN", "participated in an event", ["Person"], ["Event", "Project", "Interview"]),
        ("WAS_AFFECTED_BY", "was affected by an event", ["Person"], ["Event", "HospitalVisit", "Transaction"]),
    ],
    "Preferences": [
        ("HAS_PREFERENCE", "has a stated preference", ["Person"], ["Preference"]),
    ],
}


async def seed_person_ontology():
    """Idempotently register the full Person ontology template into type_registry (source='person')."""
    rows = build_rows(PERSON_HUB, PERSON_NODES, PERSON_EDGES, source="person")
    n = await apply_template_rows(rows, hub_name=PERSON_HUB[0])
    return {"seeded": n, "domains": len(PERSON_DOMAINS)}
