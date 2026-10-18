"""Current staged MySQL tables; historical revision metadata stays frozen."""
from migrations_mysql.precision_baseline_20261006 import build_precision_metadata
metadata = build_precision_metadata()
users = metadata.tables["users"]
itineraries = metadata.tables["itineraries"]
user_memory_states = metadata.tables["user_memory_states"]
memory_facts = metadata.tables["memory_facts"]
conversations = metadata.tables["conversations"]
conversation_memory_states = metadata.tables["conversation_memory_states"]
memory_extraction_jobs = metadata.tables["memory_extraction_jobs"]
messages = metadata.tables["messages"]
planning_briefs = metadata.tables["planning_briefs"]
active_brief_slots = metadata.tables["active_brief_slots"]
runs = metadata.tables["runs"]
run_interaction_responses = metadata.tables["run_interaction_responses"]
run_events = metadata.tables["run_events"]
itinerary_enrichments = metadata.tables["itinerary_enrichments"]
optimizer_shadow_runs = metadata.tables["optimizer_shadow_runs"]
