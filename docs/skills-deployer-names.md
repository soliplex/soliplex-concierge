# Skill names and ownership

Teams publish skill names with the Agent Skills spelling rules. A name contains
lowercase letters, digits, and single hyphens between groups, with at most
64 characters. The `soliplex-` prefix is reserved. Team slugs use the same
spelling pattern.

A team can publish `triage` without a team prefix. The first team to publish a
name keeps it. Review rejects a later publication of that name by another team
and names the owning team. Callers pass `other_names` as a mapping from each
published skill name to its owning team. Names use canonical caseless matching.

Long-term namespacing is tracked in
[soliplex/soliplex#1543](https://github.com/soliplex/soliplex/issues/1543).
