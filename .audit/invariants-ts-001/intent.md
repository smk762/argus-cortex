# argus-cortex has role 'library' but is modeled as an HTTP call target

## Night conductor remediation



### Finding
```json
{
  "id": "AI-001",
  "title": "argus-cortex has role 'library' but is modeled as an HTTP call target",
  "severity": "MEDIUM",
  "category": "service_role",
  "evidence": "service_roles: argus-cortex role=library. call_graph_rules.allowed_calls contains from '*' to argus-cortex ('Shared taxonomy / wire schemas'). service_url_env_vars maps env_fragment ARGUS_CORTEX to argus-cortex.",
  "impact": "A library should be consumed as a package dependency, not over HTTP. The wildcard edge plus the ARGUS_CORTEX URL env var legitimizes runtime calls to cortex. It also hides suite repos that hardcode taxonomy fetches instead of pinning a versioned package.",
  "recommendation": "Decide whether argus-cortex is a library or a service. If it is a library, remove the '*'->argus-cortex HTTP edge and the ARGUS_CORTEX URL mapping, and enforce consumption via package dependency (dependency_direction: services->libraries). If it is a service, change the role to match.",
  "confidence": "medium"
}
```

### Agent work pack
- URL: http://192.168.1.198:8765/audit/packs/pack-AI-001
- id: `pack-AI-001`
- Discover: `GET /audit/packs?q=<repo-or-title>`

Draft PR — do not auto-merge.

