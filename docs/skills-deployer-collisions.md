# Skill tree collision cases

Names use canonical caseless matching: NFD, casefold, then NFD again.
This covers ASCII case, composed/decomposed accents, casefold expansions
(`Straße` / `STRASSE`), and combining marks reordered by casefolding.
Files include forbidden modes so invalid entries cannot conceal collisions.

The following cases are exercised in `tests/unit/skills_deployer/test_manifest.py`:

| Exact case | Test |
| --- | --- |
| File/file aliases under the same directory, including all four normalization examples above | `test_path_collisions` |
| Complete file paths whose skill ancestors alias | `test_collisions_cover_complete_paths` |
| Skill names colliding with another team or baseline, including canonical caseless Greek matching | `test_collisions_with_other_teams_and_baseline`, `test_other_names_use_canonical_caseless_matching` |
| File versus populated directory, case and accent aliases, either traversal order | `test_file_directory_aliases` |
| File versus empty or populated directory, one diagnostic per file, either order | `test_file_directory_collision_exactly_once` |
| Empty top-level directory versus populated skill, case and accent aliases, either order | `test_empty_top_level_directory_collides_with_skill` |
| Nested populated/populated and populated/empty directory aliases, multiple sibling files, either order | `test_directory_directory_aliases` |
| Empty/empty, empty/populated, populated/empty and populated/populated directory aliases at both skill and resource levels, all four normalization examples, either order | `test_directory_occupancy_matrix` |
| Repeated descendants of an aliased directory produce one directory diagnostic | `test_directory_alias_does_not_repeat_for_descendants` |
| Identically spelled child directories under aliased parents produce no inherited directory diagnostic | `test_directory_alias_does_not_repeat_for_descendants` |
| Independent child directory aliases beneath a top-level skill alias remain visible, case and accents, either order | `test_independent_collisions_beneath_ancestor_aliases` |
| Independent child directory aliases beneath a nested directory alias remain visible, case and accents, either order | `test_independent_collisions_beneath_ancestor_aliases` |
| File/directory aliases beneath either kind of aliased ancestor remain visible, either order | `test_independent_collisions_beneath_ancestor_aliases` |
| Empty `skill.md` directory versus regular `SKILL.md`, and empty resource directory versus a regular file | `test_empty_nested_trees_are_validated` |

Directory comparisons use the normalized parent path and the current component.
An ancestor alias therefore cannot hide a new child alias. Each directory spelling
is reported once per normalized parent/component; shared descendants do not
repeat that report. Top-level directory aliases use the skill-name diagnostic.
Empty directories do not enter the file/file comparison. File/directory conflicts
use a separate pass after all directory prefixes have been collected. Identical
file basenames under different aliased ancestors can still collide as complete
file paths, separately from the ancestor directory diagnostic.
